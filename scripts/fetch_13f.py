#!/usr/bin/env python3
"""
THE 45 PROJECT — SEC EDGAR 13F-HR 自動取得スクリプト
=====================================================

概要:
  data.sec.gov の無料・APIキー不要のRESTful APIを使って、
  主要ファンドの最新13F-HR（四半期保有報告書）から保有銘柄データを取得し、
  前四半期との比較に基づいて「本当の買い増し/売り」を算出し、
  data.json の "funds[].buys" "funds[].sells" を自動生成・更新する。

  各実行で取得した保有データは data/history/{fund_id}/{filing_date}.json に
  スナップショットとして保存され、次回実行時の比較に使われる。
  リポジトリ自体が時系列データベースを兼ねる設計。

実行方法:
  python3 fetch_13f.py

前提:
  - SEC EDGARのルール上、すべてのリクエストに連絡先メールアドレスを含む
    User-Agent ヘッダーが必須。 SEC_USER_AGENT 環境変数で設定すること。
    例: export SEC_USER_AGENT="THE45Project contact@example.com"
  - レート制限: 10 requests/sec を超えないこと（このスクリプトは安全マージンを取って実装）
  - GitHub Actions上で実行する想定（ネットワークアクセスが必要）
  - data/history/ 配下のスナップショットはリポジトリにコミットされ続ける必要がある
    （これが無いと毎回「初回」判定になり、買い/売りの比較ができない）

注意:
  - 13F-HRはCUSIPでしか銘柄を識別しないため、CUSIP→ティッカー変換が必要。
    これは company_tickers.json だけでは不十分なため、簡易マッピングテーブル
    (TICKER_OVERRIDES) を併用する。本格運用では CUSIP マスタの整備を推奨。
  - 出力する日本円換算額はその時点のドル円レートで概算するため、
    厳密な金額ではなく「規模感」を示す目的の表示であることに留意。
  - 初回実行時（その四半期のスナップショットがまだ存在しない場合）は比較ができないため、
    保有額ランキングを buys の代理として表示する（has_quarter_comparison=false）。
    2回目以降の実行から、本当の増減比較に切り替わる。
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

# ===== 設定 =====

USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "THE45Project contact@example.com"  # GitHub Actions の secrets で上書きすること
)

# USD -> JPY の概算レート（本番では為替APIから取得するのが望ましい）
USD_JPY_RATE = float(os.environ.get("USD_JPY_RATE", "150"))

# 4大ファンドのCIK（Central Index Key）
FUNDS = [
    {"id": "blackrock", "name": "ブラックロック", "cik": "1364742"},
    {"id": "berkshire",  "name": "バフェット",     "cik": "1067983"},
    {"id": "vanguard",   "name": "バンガード",     "cik": "102909"},
    {"id": "soros",      "name": "ソロス",         "cik": "1029160"},
    {"id": "burry",      "name": "マイケル・バーリー", "cik": "1649339"},
    {"id": "jpmorgan",   "name": "JPモルガン",      "cik": "19617"},
    {"id": "statestreet", "name": "ステートストリート", "cik": "93751"},
    {"id": "goldmansachs", "name": "ゴールドマン・サックス", "cik": "886982"},
    {"id": "fidelity",   "name": "フィデリティ",     "cik": "315066"},
    {"id": "capitalgroup", "name": "キャピタル・グループ", "cik": "1422848"},
]

# fund_id -> 表示名（日本語）。Similarity Score等の説明文生成に使う。
FUND_DISPLAY_NAME: dict[str, str] = {f["id"]: f["name"] for f in FUNDS}

# 新規ファンド追加時、初回スタブ作成時に使う「調査済みの歴史エピソード」。
# これは断定的な未来予測ではなく、すでに確定している実際の出来事に基づく記述。
# 出典: SEC 8-K（プレスリリース）、連邦準備制度理事会の公式発表等。
NEW_FUND_TIMELINES = {
    "burry": {
        "who": "— Michael Burry / Scion Asset Management / 「世界金融危機を予見した男」",
        "intro": "<strong>バーリーは、2008年の世界金融危機が起きる2年以上前から、その崩壊を見抜いていた人物。</strong>主要9事件以外にも、こんな個別の足跡が残っています。",
        "timeline": [
            {
                "type": "event",
                "date": "2005年〜2007年",
                "region": "米国",
                "tag": "出来事",
                "title": "誰も見向きもしなかったサブプライム崩壊に、2年以上前から賭け続けた",
                "body": "2005年頃、無名のヘッジファンド運用者だったマイケル・バーリーは、米国の住宅ローン市場の質的劣化（サブプライムローン）に異常な危うさを見出した。当時、住宅市場は絶好調で、彼の警告は「頭がおかしい」と業界から嘲笑された。<strong>それでもバーリーは、住宅ローン担保証券の値下がりに賭けるクレジット・デフォルト・スワップ（CDS）を大量に購入し続けた。</strong>2007年にサブプライム問題が表面化し、2008年に世界金融危機が本格化すると、この賭けは投資家に7億ドル超、本人に1億ドル超の利益をもたらした。この実話は後に映画『マネー・ショート 華麗なる大逆転』として描かれている。<br><br><strong>※この取引はCDS（クレジット・デフォルト・スワップ）によるものであり、後述する13F報告書（株式保有のみを開示）には反映されない。</strong>以下の保有株データは、その後バーリーが運用する別の口座・別の時期の、ロング・エクイティ・ポジションに基づくもの。",
                "isEvent": True,
            },
        ],
    },
    "jpmorgan": {
        "who": "— JPMorgan Chase & Co. / 米国最大の銀行・世界的な金融グループ",
        "intro": "<strong>JPモルガンは、リーマン破綻の6ヶ月前にすでに次の危機の現場にいた会社。</strong>主要9事件以外にも、こんな個別の足跡が残っています。",
        "timeline": [
            {
                "type": "event",
                "date": "2008年3月16日",
                "region": "米国",
                "tag": "出来事",
                "title": "リーマン破綻の6ヶ月前、JPモルガンは次の危機の現場にいた",
                "body": "サブプライム問題が深刻化する中、大手投資銀行ベア・スターンズが資金繰りに行き詰まり、わずか数日でほぼ破綻状態に陥った。<strong>連邦準備制度理事会（FRB）の支援を受けながら、JPモルガンがベア・スターンズを買収・救済</strong>。この出来事は、半年後の2008年9月に起きるリーマン・ブラザーズ破綻の「前哨戦」として歴史に記録されている。リーマンが実際に破綻する6ヶ月前、JPモルガンはすでに金融システムの危うさのただ中で動いていた。",
                "isEvent": True,
            },
        ],
    },
}

DATA_JSON_PATH = Path(__file__).parent.parent / "data.json"
SNAPSHOT_DIR = Path(__file__).parent.parent / "data" / "history"
REQUEST_DELAY_SEC = 0.15  # 10 req/sec制限に対する安全マージン
TOP_N_DEFAULT = 5

# 会社名の日本語表示・desc・セクター絵文字を補完する簡易マスタ
# (本格運用ではこの部分を companies テーブルとして data.json 側に出すのが理想)
TICKER_OVERRIDES = {
    "XOM": {"name": "エクソンモービル", "desc": "世界最大級の石油会社"},
    "CVX": {"name": "シェブロン", "desc": "米国大手石油会社"},
    "NVDA": {"name": "エヌビディア", "desc": "AI半導体の王者"},
    "AAPL": {"name": "アップル", "desc": "iPhoneのメーカー"},
    "MSFT": {"name": "マイクロソフト", "desc": "WindowsとChatGPTの親会社"},
    "META": {"name": "メタ（Facebook）", "desc": "FacebookとInstagramの会社"},
    "AMZN": {"name": "アマゾン", "desc": "ネット通販とクラウドの会社"},
    "GOOGL": {"name": "グーグル", "desc": "検索エンジンとYouTubeの会社"},
    "TSLA": {"name": "テスラ", "desc": "イーロン・マスクの電気自動車会社"},
    "JPM": {"name": "JPモルガン", "desc": "米国最大の銀行"},
    "GS": {"name": "ゴールドマン・サックス", "desc": "世界最大級の投資銀行"},
    "BAC": {"name": "バンク・オブ・アメリカ", "desc": "米国大手銀行"},
    "OXY": {"name": "西洋石油（OXY）", "desc": "テキサス州の石油・天然ガス会社"},
    "KR": {"name": "クローガー", "desc": "米国大手スーパーマーケット"},
    "MCO": {"name": "ムーディーズ", "desc": "企業の信用格付け会社"},
    "AXP": {"name": "アメリカン・エキスプレス", "desc": "クレジットカード会社"},
}


def http_get(url: str, max_retries: int = 3) -> bytes:
    """SEC EDGAR向けのHTTP GET。User-Agent必須・リトライ付き。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                # レート制限に当たった場合は待って再試行
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(1 * (attempt + 1))
    raise RuntimeError(f"GET failed after {max_retries} retries: {url} ({last_err})")


def get_latest_13f_accession(cik: str) -> dict | None:
    """
    指定CIKの最新13F-HR（修正版/A以外）のaccession番号と提出日を取得。
    data.sec.gov/submissions/CIK##########.json を使用。
    """
    cik_padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    raw = http_get(url)
    data = json.loads(raw)

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    for i, form in enumerate(forms):
        if form == "13F-HR":  # /A (修正版) は除外
            return {
                "accession": accessions[i],
                "filing_date": dates[i],
                "primary_document": primary_docs[i],
            }
    return None


def get_all_13f_accessions(cik: str, max_extra_pages: int = 10) -> list[dict]:
    """
    指定CIKの「過去すべて」の13F-HR（修正版/A以外）を取得する。

    SEC EDGARの submissions API は、直近の提出物を "filings.recent" に持つが、
    提出件数が多いファイラー（BlackRock等）では古いものが
    "filings.files" に列挙される別ページ（CIK{cik}-submissions-{N}.json）に
    分割されていることがある。この関数はそれらも辿って、可能な限り過去の
    13F-HR提出をすべて収集する。

    戻り値: filing_date 昇順（古い→新しい）の dict のリスト
            [{accession, filing_date, primary_document}, ...]

    max_extra_pages: filings.files を辿る上限（無限ループ防止の安全装置）
    """
    cik_padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    raw = http_get(url)
    data = json.loads(raw)

    results = []

    def extract_13f(block: dict):
        forms = block.get("form", [])
        accessions = block.get("accessionNumber", [])
        dates = block.get("filingDate", [])
        primary_docs = block.get("primaryDocument", [])
        for i, form in enumerate(forms):
            if form == "13F-HR":
                results.append({
                    "accession": accessions[i],
                    "filing_date": dates[i],
                    "primary_document": primary_docs[i] if i < len(primary_docs) else "",
                })

    # 1. 直近分 (filings.recent)
    extract_13f(data.get("filings", {}).get("recent", {}))

    # 2. 古い分 (filings.files に列挙された追加ページ)
    extra_files = data.get("filings", {}).get("files", [])
    for idx, file_ref in enumerate(extra_files[:max_extra_pages]):
        file_name = file_ref.get("name")
        if not file_name:
            continue
        page_url = f"https://data.sec.gov/submissions/{file_name}"
        try:
            time.sleep(REQUEST_DELAY_SEC)
            page_raw = http_get(page_url)
            page_data = json.loads(page_raw)
            extract_13f(page_data)
        except Exception as e:
            print(f"    （追加ページ取得でエラー、スキップ: {file_name}: {e}）")
            continue

    # 重複排除（同じaccessionが複数ページに出ることは無いはずだが念のため）
    seen = set()
    unique_results = []
    for r in results:
        if r["accession"] not in seen:
            seen.add(r["accession"])
            unique_results.append(r)

    unique_results.sort(key=lambda r: r["filing_date"])
    return unique_results


def fetch_information_table_xml(cik: str, accession: str) -> str:
    """
    13F-HR提出フォルダから Information Table (XML) を取得する。
    フォルダ内の index.json を見て、infotable系のXMLファイルを特定する。
    """
    accession_nodash = accession.replace("-", "")
    cik_int = str(int(cik))  # ゼロパディング無しの数値文字列（Archivesパス用）
    folder_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/"
    index_url = folder_url + "index.json"

    raw = http_get(index_url)
    index_data = json.loads(raw)
    items = index_data.get("directory", {}).get("item", [])

    # Information Table の XML を特定（ファイル名に infotable や form13f が含まれることが多い）
    candidates = [
        it["name"] for it in items
        if it["name"].lower().endswith(".xml")
        and ("infotable" in it["name"].lower() or "form13f" in it["name"].lower())
    ]
    if not candidates:
        # フォールバック: xml拡張子のうち primary_doc 以外で最大サイズのものを使う
        xml_files = [it["name"] for it in items if it["name"].lower().endswith(".xml")]
        if not xml_files:
            raise RuntimeError(f"No XML found in {folder_url}")
        candidates = xml_files

    xml_url = folder_url + candidates[0]
    raw_xml = http_get(xml_url)
    return raw_xml.decode("utf-8", errors="replace")


def parse_holdings(xml_text: str, filing_date: str = "") -> list[dict]:
    """
    13F Information Table XML をパースして保有銘柄リストを返す。
    各 infoTable には nameOfIssuer, cusip, value, shrsOrPrnAmt が含まれる。

    重要: SECの仕様変更により、<value>の単位が提出日によって異なる。
      - 2023年1月3日より前の提出: value は「千ドル」単位 (×1000 が必要)
      - 2023年1月3日以降の提出:   value は「ドル」そのまま (変換不要)
      参考: SEC Form 13F Data Sets README
      (https://www.sec.gov/files/form_13f_readme.pdf)

    NOTE: SECのXMLは名前空間の書き方がファイルごとに揺れる
    （xmlns="...", xmlns:n1="...", タグに n1:infoTable のような接頭辞がつく等）。
    そのため、テキストレベルで全ての名前空間宣言とタグの接頭辞を除去してから
    パースする方式にしている（ElementTreeのns対応に頼らない）。
    """
    # 2023-01-03 以降は value がドル単位そのもの。それより前は千ドル単位。
    value_multiplier = 1
    if filing_date:
        try:
            cutoff = "2023-01-03"
            if filing_date < cutoff:
                value_multiplier = 1000
        except Exception:
            value_multiplier = 1

    text = xml_text

    # BOMやXML宣言の前の余分な空白を除去
    text = text.lstrip("\ufeff").strip()

    # 1. xmlns="..." および xmlns:接頭辞="..." の属性を全て除去
    text = re.sub(r'\s+xmlns(:\w+)?="[^"]*"', "", text)

    # 2. 残った属性の "接頭辞:属性名=" から接頭辞を除去 (例: xsi:schemaLocation= -> schemaLocation=)
    #    ※ xmlns除去後に残る xsi: などの属性接頭辞に対応。値はそのまま保持する。
    text = re.sub(r'(\s)\w+:(\w+)=', r'\1\2=', text)

    # 3. 開始/終了タグの "接頭辞:" を除去 (例: <n1:infoTable> -> <infoTable>)
    text = re.sub(r'<(/?)\w+:(\w+)', r'<\1\2', text)

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise RuntimeError(f"XML parse error after namespace stripping: {e}")

    holdings = []
    for info_table in root.iter():
        tag_local = info_table.tag.split("}")[-1]
        if tag_local != "infoTable":
            continue
        row = {}
        for child in info_table:
            tag = child.tag.split("}")[-1]
            if tag == "nameOfIssuer":
                row["name_of_issuer"] = (child.text or "").strip()
            elif tag == "titleOfClass":
                row["title_of_class"] = (child.text or "").strip()
            elif tag == "cusip":
                row["cusip"] = (child.text or "").strip()
            elif tag == "value":
                try:
                    row["value_usd"] = int((child.text or "0").strip()) * value_multiplier
                except (TypeError, ValueError):
                    row["value_usd"] = 0
            elif tag == "shrsOrPrnAmt":
                for sub in child:
                    sub_tag = sub.tag.split("}")[-1]
                    if sub_tag == "sshPrnamt":
                        try:
                            row["shares"] = int((sub.text or "0").strip())
                        except (TypeError, ValueError):
                            row["shares"] = 0
        if row.get("name_of_issuer"):
            holdings.append(row)
    return holdings


def to_jpy_label(value_usd: int) -> str:
    """USD金額を日本語の兆円/億円表示に変換する。"""
    jpy = value_usd * USD_JPY_RATE
    if abs(jpy) >= 1_0000_0000_0000:  # 1兆円以上
        return f"{jpy / 1_0000_0000_0000:.1f}兆円"
    else:
        return f"{jpy / 1_0000_0000:.0f}億円"


def aggregate_by_cusip(holdings: list[dict]) -> list[dict]:
    """
    同じ銘柄(CUSIP)が複数行に分かれて報告されている場合（株式クラス違い・
    複数口座区分など）、value_usd と shares を合算して1行にまとめる。
    """
    merged: dict[str, dict] = {}
    for h in holdings:
        cusip = h.get("cusip", "")
        if not cusip:
            continue
        if cusip not in merged:
            merged[cusip] = {
                "name_of_issuer": h.get("name_of_issuer", "UNKNOWN"),
                "title_of_class": h.get("title_of_class", ""),
                "cusip": cusip,
                "value_usd": 0,
                "shares": 0,
            }
        merged[cusip]["value_usd"] += h.get("value_usd", 0)
        merged[cusip]["shares"] += h.get("shares", 0)
    return list(merged.values())


def snapshot_path(fund_id: str, filing_date: str) -> Path:
    """フォルダ data/history/{fund_id}/{filing_date}.json のパスを返す。"""
    return SNAPSHOT_DIR / fund_id / f"{filing_date}.json"


def save_snapshot(fund_id: str, filing_date: str, holdings: list[dict]):
    """今回取得した保有データ（CUSIP単位に集約済み）をスナップショットとして保存する。"""
    path = snapshot_path(fund_id, filing_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"fund_id": fund_id, "filing_date": filing_date, "holdings": holdings},
            f, ensure_ascii=False, indent=2
        )


def load_previous_snapshot(fund_id: str, current_filing_date: str) -> dict | None:
    """
    指定ファンドの過去スナップショットのうち、current_filing_date より前で
    最も新しいものを読み込む。無ければ None。
    """
    fund_dir = SNAPSHOT_DIR / fund_id
    if not fund_dir.exists():
        return None

    candidates = []
    for p in fund_dir.glob("*.json"):
        date_str = p.stem  # ファイル名 = filing_date (YYYY-MM-DD)
        if date_str < current_filing_date:
            candidates.append((date_str, p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, latest_path = candidates[0]
    with open(latest_path, "r", encoding="utf-8") as f:
        return json.load(f)


_SNAPSHOT_CACHE: dict[str, list[dict]] = {}


def load_all_snapshots(fund_id: str) -> list[dict]:
    """
    指定ファンドの全スナップショットを filing_date の昇順（古い→新しい）で読み込む。
    トレンド計算（四半期を跨いだ推移）の元データとして使う。

    NOTE（パフォーマンス）: この関数は呼び出される頻度が非常に高い
    （Similarity Score・Early Movement Detection等、複数の機能から
    同じファンドのデータを何百回も参照する）。スクリプト実行中は
    スナップショットファイルの内容が変わらないため、プロセス内メモリに
    キャッシュし、同じファンドのディスク読み込み・JSON解析を1回だけに
    抑える（以前はキャッシュが無く、これが原因で実行時間が大幅に
    膨らんでいた）。
    """
    if fund_id in _SNAPSHOT_CACHE:
        return _SNAPSHOT_CACHE[fund_id]

    fund_dir = SNAPSHOT_DIR / fund_id
    if not fund_dir.exists():
        _SNAPSHOT_CACHE[fund_id] = []
        return []

    snapshots = []
    for p in sorted(fund_dir.glob("*.json"), key=lambda x: x.stem):
        with open(p, "r", encoding="utf-8") as f:
            snapshots.append(json.load(f))

    _SNAPSHOT_CACHE[fund_id] = snapshots
    return snapshots


def compute_fund_trend(fund_id: str) -> dict:
    """
    指定ファンドの全スナップショットから、四半期ごとの集計指標（トレンド）を計算する。

    各四半期について以下を計算:
      - total_value_usd: 報告された保有銘柄の合計額（13F記載分のみ。現金等は含まれない）
      - position_count: 保有銘柄数（CUSIP単位で集約後）
      - top5_concentration_pct: 上位5銘柄が合計額の何%を占めるか（集中度の指標）

    NOTE: 13F-HRはロング・エクイティ・ポジションのみを開示するものであり、
    現金やショートポジションは含まれない。「現金比率」のような指標は13Fだけからは
    算出できないため、ここでは計算可能な指標（集中度・銘柄数・合計額）のみを扱う。

    戻り値: { "fund_id": ..., "quarters": [ {filing_date, total_value_usd, position_count,
              top5_concentration_pct}, ... ], "has_enough_history": bool }
      has_enough_history は2四半期以上のデータが無いと False
      （トレンド＝変化を見せるには最低2点が必要なため）
    """
    snapshots = load_all_snapshots(fund_id)
    quarters = []

    for snap in snapshots:
        holdings = snap.get("holdings", [])
        if not holdings:
            continue
        total_value = sum(h.get("value_usd", 0) for h in holdings)
        position_count = len(holdings)
        sorted_h = sorted(holdings, key=lambda h: h.get("value_usd", 0), reverse=True)
        top5_value = sum(h.get("value_usd", 0) for h in sorted_h[:5])
        top5_pct = round((top5_value / total_value) * 100, 1) if total_value else 0.0

        quarters.append({
            "filing_date": snap.get("filing_date", ""),
            "total_value_usd": total_value,
            "total_value_label": to_jpy_label(total_value),
            "position_count": position_count,
            "top5_concentration_pct": top5_pct,
        })

    return {
        "fund_id": fund_id,
        "quarters": quarters,
        "has_enough_history": len(quarters) >= 2,
    }


# ===== 過去の参照局面（類似度計算用） =====
# NOTE: 2013年より前の事件（2008年リーマン等）はSECがXML構造化13F-HRを
# 義務化する前のデータ形式のため、parse_holdings()では取得できない。
# よって参照局面は2013年以降に実際に起きた、データ取得可能な事件に限定する。
REFERENCE_EVENTS = [
    {
        "id": "covid_2020",
        "label": "2020年のコロナショック直前",
        "before_hint": "2020-02",
        "after_hint": "2020-08",
    },
    {
        "id": "svb_2023",
        "label": "2023年のSVB破綻直前",
        "before_hint": "2023-02",
        "after_hint": "2023-08",
    },
    {
        "id": "yen_carry_2024",
        "label": "2024年の円キャリートレード巻き戻し直前",
        "before_hint": "2024-05",
        "after_hint": "2024-11",
    },
]


def find_closest_snapshot(fund_id: str, target_date_hint: str) -> dict | None:
    """
    指定ファンドのスナップショットの中で、target_date_hint（"YYYY-MM"形式）に
    最も近い filing_date を持つものを返す。
    """
    snapshots = load_all_snapshots(fund_id)
    if not snapshots:
        return None

    target_ym = target_date_hint[:7]  # "YYYY-MM"

    def date_distance(snap):
        fd = snap.get("filing_date", "")
        if not fd:
            return float("inf")
        # 文字列としての日付差（YYYY-MM-DD同士の比較なので、月単位の近さを文字列差で近似）
        try:
            from datetime import date
            y1, m1 = int(target_ym[:4]), int(target_ym[5:7])
            y2, m2 = int(fd[:4]), int(fd[5:7])
            return abs((y1 * 12 + m1) - (y2 * 12 + m2))
        except (ValueError, IndexError):
            return float("inf")

    closest = min(snapshots, key=date_distance)
    # 半年以上離れていたら「近い」とは言えないので None を返す
    if date_distance(closest) > 6:
        return None
    return closest


def compute_historical_event_moves(fund_id: str, event: dict, top_n: int = 1) -> dict | None:
    """
    指定の過去事件（event）の前後で、ファンドが実際に何を買い・売りしたかを計算する。
    これは「予測」ではなく、すでに確定した過去の実際の行動の記録。

    戻り値: {
      "event_id", "event_label",
      "before_filing_date", "after_filing_date",
      "before_concentration_pct",  # 類似度計算に使う、当時の集中度
      "top_buy": {name, cusip, value_label} or None,
      "top_sell": {name, cusip, value_label} or None,
    }
    十分なデータが無い場合は None。
    """
    before_snap = find_closest_snapshot(fund_id, event["before_hint"])
    after_snap = find_closest_snapshot(fund_id, event["after_hint"])

    if not before_snap or not after_snap:
        return None
    if before_snap["filing_date"] == after_snap["filing_date"]:
        return None  # 同じスナップショットしか見つからなかった場合は無効

    before_holdings = before_snap.get("holdings", [])
    after_holdings = after_snap.get("holdings", [])
    if not before_holdings or not after_holdings:
        return None

    diffs = compute_diff(after_holdings, before_holdings)
    increases = sorted([d for d in diffs if d["delta_usd"] > 0], key=lambda d: d["delta_usd"], reverse=True)
    decreases = sorted([d for d in diffs if d["delta_usd"] < 0], key=lambda d: d["delta_usd"])

    # 当時の集中度（類似度比較に使う）
    total_value = sum(h.get("value_usd", 0) for h in before_holdings)
    sorted_before = sorted(before_holdings, key=lambda h: h.get("value_usd", 0), reverse=True)
    top5_value = sum(h.get("value_usd", 0) for h in sorted_before[:5])
    before_concentration = round((top5_value / total_value) * 100, 1) if total_value else 0.0

    def pick_top(lst):
        if not lst:
            return None
        h = lst[0]
        class_suffix = extract_class_suffix(h.get("title_of_class", ""))
        name = h.get("name_of_issuer", "UNKNOWN").title()
        if class_suffix:
            name = f"{name} ({class_suffix})"
        return {
            "name": name,
            "cusip": h.get("cusip", ""),
            "value_label": to_jpy_label(abs(h.get("delta_usd", 0))),
        }

    return {
        "event_id": event["id"],
        "event_label": event["label"],
        "before_filing_date": before_snap["filing_date"],
        "after_filing_date": after_snap["filing_date"],
        "before_concentration_pct": before_concentration,
        "top_buy": pick_top(increases),
        "top_sell": pick_top(decreases),
    }


def compute_pattern_similarity(fund_id: str) -> dict | None:
    """
    現在のファンドの配置（直近の集中度）と、過去の参照局面（REFERENCE_EVENTS）の
    集中度を比較し、最も近い局面を「類似度」として返す。

    NOTE: これは「将来の予測」ではなく、「過去のパターンとの統計的な近さ」のみを
    示す。表示の際は「○○の確率で危機が起きる」という解釈を加えてはならない。

    類似度の計算: 集中度の差をシンプルな割合で近似（1 - |差| / 基準値）。
    本格的なZ-score正規化・コサイン類似度は、より多くの指標（セクター別配分等）が
    揃ってから拡張する。現時点では「集中度」という1指標での簡易近似。

    戻り値: { "most_similar_event": {...}, "similarity_pct": float,
              "current_concentration_pct": float } または None
    """
    trend = compute_fund_trend(fund_id)
    if not trend["has_enough_history"]:
        return None

    current_concentration = trend["quarters"][-1]["top5_concentration_pct"]

    best_match = None
    best_similarity = -1

    for event in REFERENCE_EVENTS:
        moves = compute_historical_event_moves(fund_id, event)
        if not moves:
            continue

        diff = abs(current_concentration - moves["before_concentration_pct"])
        # 差が0%なら100%類似、差が大きいほど類似度が下がる（差20%で0%類似になるよう線形近似）
        similarity = max(0, 100 - (diff / 20 * 100))

        if similarity > best_similarity:
            best_similarity = similarity
            best_match = moves

    if best_match is None:
        return None

    return {
        "most_similar_event": best_match,
        "similarity_pct": round(best_similarity, 1),
        "current_concentration_pct": current_concentration,
    }


def compute_diff(current_holdings: list[dict], previous_holdings: list[dict] | None) -> list[dict]:
    """
    前回スナップショットとの比較で、各銘柄の増減額(delta_usd)を計算する。
    前回データが無い場合は delta_usd = value_usd（全額が「新規」扱い）として返す
    （= 比較データがないことを呼び出し側で判定できるよう is_first_snapshot を付与）。
    """
    prev_map = {}
    if previous_holdings:
        for h in previous_holdings:
            cusip = h.get("cusip", "")
            if cusip:
                prev_map[cusip] = h.get("value_usd", 0)

    diffs = []
    current_cusips = set()
    for h in current_holdings:
        cusip = h.get("cusip", "")
        if not cusip:
            continue
        current_cusips.add(cusip)
        prev_value = prev_map.get(cusip, 0)
        delta = h.get("value_usd", 0) - prev_value
        diffs.append({
            **h,
            "delta_usd": delta,
            "is_new_position": cusip not in prev_map,
        })

    # 前回あったが今回完全に消えた銘柄 = 全売却
    if previous_holdings:
        for cusip, prev_value in prev_map.items():
            if cusip not in current_cusips:
                # 元の銘柄名を前回データから引く
                name = next(
                    (h.get("name_of_issuer", "UNKNOWN") for h in previous_holdings if h.get("cusip") == cusip),
                    "UNKNOWN"
                )
                title_of_class = next(
                    (h.get("title_of_class", "") for h in previous_holdings if h.get("cusip") == cusip),
                    ""
                )
                diffs.append({
                    "name_of_issuer": name,
                    "title_of_class": title_of_class,
                    "cusip": cusip,
                    "value_usd": 0,
                    "shares": 0,
                    "delta_usd": -prev_value,
                    "is_exited_position": True,
                })

    return diffs


EXTENDED_N_DEFAULT = 30  # 有料解放時に見せる拡張リストの件数


def build_fund_holdings(
    holdings: list[dict],
    previous_holdings: list[dict] | None,
    top_n: int = TOP_N_DEFAULT,
    extended_n: int = EXTENDED_N_DEFAULT,
) -> dict:
    """
    保有銘柄リストから「買い（増加上位）」「売り（減少上位）」を構築する。

    戻り値は dict:
      - buys / sells: 無料表示用 (top_n件、通常5件)
      - buys_extended / sells_extended: 有料解放用の拡張リスト (extended_n件、通常30件)
      - has_comparison: 前四半期比較ができたかどうか

    previous_holdings が None の場合（初回実行・履歴なし）は、
    比較ができないため "保有額が大きい銘柄" を buys の代理として返し、
    sells は空にする。
    """
    holdings = aggregate_by_cusip(holdings)

    if previous_holdings is None:
        # 初回実行: 比較不可。保有額ベースの簡易表示にフォールバック。
        sorted_holdings = sorted(holdings, key=lambda h: h.get("value_usd", 0), reverse=True)
        top = sorted_holdings[:top_n]
        extended = sorted_holdings[:extended_n]
        max_value = top[0]["value_usd"] if top else 1
        max_value_ext = extended[0]["value_usd"] if extended else 1

        buys = [_format_holding_row(h, max_value, is_increase=True) for h in top]
        buys_extended = [_format_holding_row(h, max_value_ext, is_increase=True) for h in extended]

        return {
            "buys": buys,
            "sells": [],
            "buys_extended": buys_extended,
            "sells_extended": [],
            "has_comparison": False,
        }

    diffs = compute_diff(holdings, previous_holdings)

    increases = sorted([d for d in diffs if d["delta_usd"] > 0], key=lambda d: d["delta_usd"], reverse=True)
    decreases = sorted([d for d in diffs if d["delta_usd"] < 0], key=lambda d: d["delta_usd"])

    top_buys = increases[:top_n]
    top_sells = decreases[:top_n]
    ext_buys = increases[:extended_n]
    ext_sells = decreases[:extended_n]

    max_buy = top_buys[0]["delta_usd"] if top_buys else 1
    max_sell = abs(top_sells[0]["delta_usd"]) if top_sells else 1
    max_buy_ext = ext_buys[0]["delta_usd"] if ext_buys else 1
    max_sell_ext = abs(ext_sells[0]["delta_usd"]) if ext_sells else 1

    buys = [_format_holding_row(h, max_buy, is_increase=True, use_delta=True) for h in top_buys]
    sells = [_format_holding_row(h, max_sell, is_increase=False, use_delta=True) for h in top_sells]
    buys_extended = [_format_holding_row(h, max_buy_ext, is_increase=True, use_delta=True) for h in ext_buys]
    sells_extended = [_format_holding_row(h, max_sell_ext, is_increase=False, use_delta=True) for h in ext_sells]

    return {
        "buys": buys,
        "sells": sells,
        "buys_extended": buys_extended,
        "sells_extended": sells_extended,
        "has_comparison": True,
    }


def extract_class_suffix(title_of_class: str) -> str:
    """
    titleOfClass文字列から、株式クラス（A/B/C等）の表示用サフィックスを抽出する。
    例: "COM CL A" -> "Class A", "COM" -> "" (無印の普通株はサフィックス不要)

    これは Alphabet (GOOGL/Class A, GOOG/Class C) のように、同じ会社が
    複数のCUSIPで別クラスの株式を発行している場合に、表示上「同じ名前が
    重複しているように見える」問題（バグに見える）を解消するために使う。
    """
    if not title_of_class:
        return ""
    match = re.search(r'\bCL\s*([A-Z])\b', title_of_class.upper())
    if match:
        return f"Class {match.group(1)}"
    return ""


def _format_holding_row(h: dict, max_value: int, is_increase: bool, use_delta: bool = False) -> dict:
    """保有/増減データ1件を、サイト表示用のdict形式に整形する。"""
    cusip = h.get("cusip", "")
    name_raw = h.get("name_of_issuer", "UNKNOWN")
    override = TICKER_OVERRIDES.get(cusip, None)
    display_name = override["name"] if override else name_raw.title()

    class_suffix = extract_class_suffix(h.get("title_of_class", ""))
    if class_suffix:
        display_name = f"{display_name} ({class_suffix})"

    desc = override["desc"] if override else "SEC 13F-HR 開示銘柄"

    amount = h.get("delta_usd", h.get("value_usd", 0)) if use_delta else h.get("value_usd", 0)
    amount_abs = abs(amount)
    bar = round((amount_abs / max_value) * 100) if max_value else 0

    sign = "+" if is_increase else "-"
    tag_note = ""
    if h.get("is_new_position"):
        tag_note = "（新規）"
    elif h.get("is_exited_position"):
        tag_note = "（全売却）"

    return {
        "name": display_name,
        "ticker": cusip,
        "desc": desc + tag_note,
        "value": f"{sign}約{to_jpy_label(amount_abs)}",
        "bar": max(bar, 5),
        "amount_usd_raw": amount,  # 符号付きの生のUSD額。複数ファンド間での集計計算に使う。
    }


# ===== セクター分類マスタ =====
# CUSIP -> セクターID。実際に各ファンドの保有データに出現が確認できた企業のみ、
# 手動で分類している（推測・拡大解釈はしない）。
# 未分類のCUSIPは集計から「未分類」として正直に除外・件数表示する。
SECTOR_MAP: dict[str, str] = {
    # テクノロジー（ソフトウェア・ハードウェア・半導体・インターネット）
    "67066G104": "tech",   # Nvidia
    "037833100": "tech",   # Apple
    "594918104": "tech",   # Microsoft
    "02079K305": "tech",   # Alphabet Class A
    "02079K107": "tech",   # Alphabet Class C
    "458140100": "tech",   # Intel
    "79466L302": "tech",   # Salesforce
    "595112103": "tech",   # Micron
    "68389X105": "tech",   # Oracle
    "30303M102": "tech",   # Meta Platforms
    "874039100": "tech",   # Taiwan Semiconductor
    "21873S108": "tech",   # Coreweave
    "023135106": "tech",   # Amazon
    "20717MAB9": "tech",   # Confluent
    "26210CAC8": "tech",   # Dropbox

    # 石油・エネルギー
    "723787107": "energy", # Pioneer Natural Resources
    "674599105": "energy", # Occidental Petroleum
    "166764100": "energy", # Chevron
    "30231G102": "energy", # Exxon Mobil

    # 金融
    "025816109": "finance", # American Express
    "060505104": "finance", # Bank of America
    "92826C839": "finance", # Visa

    # ヘルスケア・製薬
    "532457108": "healthcare", # Eli Lilly

    # メディア・エンタメ（ストリーミング・配信）
    "254687106": "media",   # Disney
    "64110L106": "media",   # Netflix
    "84921RAB6": "media",   # Spotify

    # 消費財・小売
    "437076102": "retail",  # Home Depot
    "191216100": "retail",  # Coca-Cola（消費財として分類）

    # 航空・運輸
    "247361702": "transport", # Delta Air Lines
}

# 表示用ラベル。「将来追加予定」のカテゴリも設計上は残しておくが、
# 実データが無い間は表示しない（is_active=Falseのものは非表示対象）。
SECTOR_LABELS: dict[str, dict] = {
    "tech":      {"label": "テクノロジー", "is_active": True},
    "energy":    {"label": "石油・エネルギー", "is_active": True},
    "finance":   {"label": "金融", "is_active": True},
    "healthcare": {"label": "ヘルスケア・製薬", "is_active": True},
    "media":     {"label": "メディア・エンタメ", "is_active": True},
    "retail":    {"label": "消費財・小売", "is_active": True},
    "transport":  {"label": "航空・運輸", "is_active": True},
    # 将来、実データが確認された場合に追加するカテゴリ（現時点では非表示）
    "gold":      {"label": "金（ゴールド）", "is_active": False},
    "realestate": {"label": "不動産・REIT", "is_active": False},
    "emerging":  {"label": "中国・新興国", "is_active": False},
    "defense":   {"label": "防衛・軍事関連", "is_active": False},
}


def compute_sector_breakdown() -> dict:
    """
    SECTOR_MAP に基づき、各ファンドの buys_extended / sells_extended を
    セクター単位で集計する。「無料エリアで今の市場全体の傾向を見せる」ための、
    実データに基づくセクターマップ用の計算。

    NOTE: SECTOR_MAP に存在しないCUSIPは「未分類」として件数のみ集計し、
    セクター別の内訳には含めない（誤った分類を避けるための誠実さ優先）。

    戻り値: {
      "sectors": [ {sector_id, label, company_count, buy_count, sell_count, buy_pct}, ... ],
      "unclassified_count": 未分類の銘柄数,
      "total_classified": 分類済みの銘柄数,
    }
    """
    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # sector_id -> { companies: set(cusip), buy_count: int, sell_count: int }
    sector_tally: dict[str, dict] = {}
    unclassified_cusips: set[str] = set()
    classified_cusips: set[str] = set()

    for fund in data["funds"]:
        for h in fund.get("buys_extended", []):
            cusip = h.get("ticker", "")
            if not cusip:
                continue
            sector_id = SECTOR_MAP.get(cusip)
            if sector_id is None:
                unclassified_cusips.add(cusip)
                continue
            classified_cusips.add(cusip)
            t = sector_tally.setdefault(sector_id, {"companies": set(), "buy_count": 0, "sell_count": 0})
            t["companies"].add(cusip)
            t["buy_count"] += 1
        for h in fund.get("sells_extended", []):
            cusip = h.get("ticker", "")
            if not cusip:
                continue
            sector_id = SECTOR_MAP.get(cusip)
            if sector_id is None:
                unclassified_cusips.add(cusip)
                continue
            classified_cusips.add(cusip)
            t = sector_tally.setdefault(sector_id, {"companies": set(), "buy_count": 0, "sell_count": 0})
            t["companies"].add(cusip)
            t["sell_count"] += 1

    sectors = []
    for sector_id, t in sector_tally.items():
        meta = SECTOR_LABELS.get(sector_id, {"label": sector_id, "is_active": True})
        total = t["buy_count"] + t["sell_count"]
        buy_pct = round((t["buy_count"] / total) * 100) if total else 0
        sectors.append({
            "sector_id": sector_id,
            "label": meta["label"],
            "company_count": len(t["companies"]),
            "buy_count": t["buy_count"],
            "sell_count": t["sell_count"],
            "buy_pct": buy_pct,
        })

    # 該当社数が多い順
    sectors.sort(key=lambda s: -s["company_count"])

    return {
        "sectors": sectors,
        "unclassified_count": len(unclassified_cusips),
        "total_classified": len(classified_cusips),
    }


def update_sector_breakdown_in_data_json():
    """compute_sector_breakdown() の結果を data.json のトップレベルに保存する。"""
    result = compute_sector_breakdown()

    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["sector_breakdown_computed"] = result
    data["_meta"]["last_updated"] = time.strftime("%Y-%m-%d")

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return result


def find_first_appearance(fund_id: str, cusip: str) -> dict | None:
    """
    指定ファンドが、追跡履歴の中で初めて指定銘柄(CUSIP)を保有した
    （value_usd > 0で記録された）四半期を返す。

    NOTE: これは「絶対的に最初に買った日」ではなく、「このサイトが追跡している
    履歴の範囲内で、最初に確認できた時点」。SEC EDGARの構造化13F-HRは2013年頃
    以降のものしか取得できないため、それより前から保有していた可能性は排除できない。
    この前提を表示時に明示する必要がある。

    戻り値: {"filing_date": "YYYY-MM-DD"} または None（一度も保有が確認できない場合）
    """
    snapshots = load_all_snapshots(fund_id)  # 古い→新しい の順
    for snap in snapshots:
        holdings = snap.get("holdings", [])
        for h in holdings:
            if h.get("cusip") == cusip and h.get("value_usd", 0) > 0:
                return {"filing_date": snap.get("filing_date", "")}
    return None


def _date_diff_days(date_a: str, date_b: str) -> int | None:
    """'YYYY-MM-DD'形式の2つの日付の差（日数、date_b - date_a）を返す。"""
    try:
        from datetime import date
        y1, m1, d1 = int(date_a[:4]), int(date_a[5:7]), int(date_a[8:10])
        y2, m2, d2 = int(date_b[:4]), int(date_b[5:7]), int(date_b[8:10])
        return (date(y2, m2, d2) - date(y1, m1, d1)).days
    except (ValueError, IndexError):
        return None


def compute_first_movers(signals: list[dict]) -> list[dict]:
    """
    一致シグナル（compute_consensus_signalsの出力）それぞれについて、
    関与している各ファンドの「この銘柄を最初に保有し始めた四半期」を比較し、
    最も早く保有を始めていたファンドを特定する。

    これは「Early Movement Detection」（THE45 Premium機能の核）の計算ロジック。
    断定的な「予測」ではなく、確定済みの過去の保有履歴の比較である点に留意。

    戻り値: signals の各要素に "first_mover" フィールドを追加したリスト。
      first_mover: {
        "fund_id": 最も早く保有開始していたファンドのID,
        "since": そのファンドの初出現日,
        "others": [{"fund_id":..., "since":..., "lag_days": 先行者からの遅れ日数}, ...]
      }
      初出現データが計算できなかった場合は None。
    """
    enriched = []
    for s in signals:
        entries = []
        for fid in s["fund_ids"]:
            first = find_first_appearance(fid, s["cusip"])
            if first:
                entries.append({"fund_id": fid, "since": first["filing_date"]})

        first_mover = None
        if len(entries) >= 2:
            entries.sort(key=lambda e: e["since"])
            earliest = entries[0]
            others = []
            for e in entries[1:]:
                lag_days = _date_diff_days(earliest["since"], e["since"])
                others.append({"fund_id": e["fund_id"], "since": e["since"], "lag_days": lag_days})
            first_mover = {
                "fund_id": earliest["fund_id"],
                "since": earliest["since"],
                "others": others,
            }

        enriched.append({**s, "first_mover": first_mover})

    return enriched


def build_global_first_appearance_map() -> dict[str, dict[str, str]]:
    """
    追跡中の全ファンド × 全銘柄について、「いつ初めてその銘柄の保有が
    確認されたか」を一括計算する。「Has this pattern happened before?」
    （そのファンドの先行実績そのもの）を計算するための基礎データ。

    戻り値: { cusip: { fund_id: filing_date, ... }, ... }

    NOTE: 計算コストを抑えるため、各ファンドのスナップショットを古い順に
    1回だけ走査し、銘柄ごとに「最初に出てきた時点」だけを記録する
    （2回目以降の出現は無視する）。
    """
    result: dict[str, dict[str, str]] = {}
    for fund in FUNDS:
        fund_id = fund["id"]
        snapshots = load_all_snapshots(fund_id)  # 古い→新しい
        seen_cusips: set[str] = set()
        for snap in snapshots:
            filing_date = snap.get("filing_date", "")
            for h in snap.get("holdings", []):
                cusip = h.get("cusip", "")
                if not cusip or h.get("value_usd", 0) <= 0:
                    continue
                if cusip in seen_cusips:
                    continue
                seen_cusips.add(cusip)
                result.setdefault(cusip, {})[fund_id] = filing_date
    return result


def compute_fund_leadership_track_record(fund_id: str, first_appearance_map: dict) -> dict:
    """
    指定ファンドについて、過去に他の追跡対象ファンドより先に銘柄の保有を
    始めていた（その後他のファンドも追随した）ケースを集計する。

    「Has this pattern happened before?」に答える、Premium機能の核。
    断定的な予測ではなく、すでに確定している過去の保有開始タイミングの比較。

    戻り値: {
      "fund_id": ...,
      "led_count": 自分が最も早く保有を始めていた回数（他社が後から追随した回数）,
      "followed_count": 他社が先に保有し、自分が後から入った回数,
      "avg_lag_days": 自分が先行した場合、他社が平均何日後に追随したか（None可）,
    }
    """
    led_lags: list[float] = []
    followed_count = 0

    for cusip, fund_dates in first_appearance_map.items():
        if fund_id not in fund_dates or len(fund_dates) < 2:
            continue

        my_date = fund_dates[fund_id]
        other_dates = [d for fid, d in fund_dates.items() if fid != fund_id]

        is_earliest = all(my_date <= d for d in other_dates)
        if is_earliest:
            lags = [lag for d in other_dates if (lag := _date_diff_days(my_date, d)) is not None and lag > 0]
            led_lags.extend(lags)
        else:
            followed_count += 1

    led_count = len(led_lags)
    avg_lag = round(sum(led_lags) / led_count) if led_count else None

    return {
        "fund_id": fund_id,
        "led_count": led_count,
        "followed_count": followed_count,
        "avg_lag_days": avg_lag,
    }


def compute_fund_history_profile(fund_id: str, first_appearance_map: dict) -> dict:
    """
    指定ファンドの「行動履歴プロフィール」を計算する。Fund History機能の核。

    単に「過去の保有変更履歴を一覧表示する」のではなく、このファンドが
    過去どのような行動パターンを取ってきたか（先行する傾向があるか、
    どの分野で先行することが多いか）という、比較から意味を読み取れる
    形にまとめる。

    NOTE: compute_fund_leadership_track_record() を土台に、分野別の
    先行傾向を追加したもの。断定的な評価（「優れている」等）は行わず、
    観測された傾向の記述にとどめる。

    戻り値: {
      "fund_id": ...,
      "led_count": 先行していた回数,
      "followed_count": 他社が先行し、後から入った回数,
      "avg_lag_days": 先行した場合、他社が平均何日後に追随したか,
      "sector_lead_counts": { sector_id: 先行回数, ... }  # 分野別の先行傾向
    }
    """
    base = compute_fund_leadership_track_record(fund_id, first_appearance_map)

    # 分野別の先行傾向: このファンドが先行していた銘柄を分野ごとに数える
    sector_lead_counts: dict[str, int] = {}
    for cusip, fund_dates in first_appearance_map.items():
        if fund_id not in fund_dates or len(fund_dates) < 2:
            continue
        my_date = fund_dates[fund_id]
        other_dates = [d for fid, d in fund_dates.items() if fid != fund_id]
        is_earliest = all(my_date <= d for d in other_dates)
        if not is_earliest:
            continue
        sector_id = SECTOR_MAP.get(cusip)
        if sector_id is None:
            continue
        sector_lead_counts[sector_id] = sector_lead_counts.get(sector_id, 0) + 1

    return {**base, "sector_lead_counts": sector_lead_counts}


def update_fund_history_profiles_in_data_json(first_appearance_map: dict, data: dict) -> None:
    """
    全追跡対象ファンドについて compute_fund_history_profile() を計算し、
    data["funds"] の各ファンドオブジェクトに "history_profile" として保存する。
    """
    for fund in FUNDS:
        fid = fund["id"]
        profile = compute_fund_history_profile(fid, first_appearance_map)
        for f_entry in data["funds"]:
            if f_entry["id"] == fid:
                f_entry["history_profile"] = profile
                break


def get_fund_cusip_value_at(fund_id: str, cusip: str, filing_date: str) -> int:
    """指定ファンドの、指定日時点での指定銘柄の保有額(USD)を返す。保有が無ければ0。"""
    snapshots = load_all_snapshots(fund_id)
    for snap in snapshots:
        if snap.get("filing_date") == filing_date:
            for h in snap.get("holdings", []):
                if h.get("cusip") == cusip:
                    return h.get("value_usd", 0)
    return 0


def get_fund_cusip_value_history(fund_id: str, cusip: str) -> list[dict]:
    """
    指定ファンドの、指定銘柄(CUSIP)についての保有額の時系列を返す。
    [{"filing_date": ..., "value_usd": ...}, ...]  古い→新しい順。
    保有していない四半期は value_usd=0 として記録される。
    """
    snapshots = load_all_snapshots(fund_id)  # 古い→新しい
    history = []
    for snap in snapshots:
        filing_date = snap.get("filing_date", "")
        value = 0
        for h in snap.get("holdings", []):
            if h.get("cusip") == cusip:
                value = h.get("value_usd", 0)
                break
        history.append({"filing_date": filing_date, "value_usd": value})
    return history


def compute_company_adoption_archive(cusip: str, first_appearance_map: dict) -> dict:
    """
    指定銘柄(CUSIP)について、追跡対象の全ファンドが「いつ初めて保有を
    始めたか」を時系列で並べた、企業単位の採用順アーカイブ。

    「Similar Event Archive」（Has this pattern happened before?）の核。
    今回の一致シグナルに関与している2〜4ファンドだけでなく、過去に同じ
    銘柄へ参入した全ファンドの順序を示すことで、「このパターンが過去にも
    あったか」「他にどのファンドが関わっていたか」を確認できるようにする。

    NOTE: 因果関係は主張しない。「関連した動き」「その後確認された変化」
    という事実の提示にとどめる。

    戻り値: {
      "cusip": ...,
      "entries": [{"fund_id":..., "since":...}, ...],  # 参入順（古い→新しい）
      "total_funds_ever_held": この銘柄を保有したことがある追跡対象ファンドの総数,
    }
    """
    fund_dates = first_appearance_map.get(cusip, {})
    entries = sorted(
        [{"fund_id": fid, "since": d} for fid, d in fund_dates.items()],
        key=lambda e: e["since"]
    )
    return {
        "cusip": cusip,
        "entries": entries,
        "total_funds_ever_held": len(entries),
    }


# ===== Similarity Score 基盤（Step 1: 過去の任意時点の一致シグナルを再現する） =====
# NOTE: これは Similarity Score 自体の計算ではなく、その比較対象となる
# 「過去のある時点で、何が一致シグナルとして検出されていたか」を作るための
# 土台。スコア自体（Step 2）は、この関数の出力同士を比較して構築する。

def get_snapshot_and_previous(fund_id: str, near_date: str, max_distance_months: int = 4) -> tuple[dict | None, dict | None]:
    """
    指定ファンドの、near_date に最も近いスナップショットと、その直前
    （前四半期比較用）のスナップショットを返す。

    戻り値: (該当スナップショット, その直前のスナップショット)
            該当が見つからない、または直前データが無い場合は None を含む。
    """
    snapshots = load_all_snapshots(fund_id)  # 古い→新しい
    if not snapshots:
        return None, None

    def dist(snap):
        d = _date_diff_days(near_date, snap.get("filing_date", ""))
        return abs(d) if d is not None else float("inf")

    closest_idx = min(range(len(snapshots)), key=lambda i: dist(snapshots[i]))
    if dist(snapshots[closest_idx]) > max_distance_months * 31:
        return None, None  # near_dateから離れすぎている場合は無効

    if closest_idx == 0:
        return snapshots[closest_idx], None

    return snapshots[closest_idx], snapshots[closest_idx - 1]


def compute_historical_consensus_signals(target_filing_date: str, top_n: int = 45) -> dict:
    """
    指定した過去の時点（target_filing_date付近）における「複数ファンド
    一致シグナル」を、compute_consensus_signals() と同じロジックで再計算する。

    これにより「今回の一致シグナルと、過去のある四半期の一致シグナルが
    どれだけ似ていたか」を比較できるようになる（Similarity Scoreの土台）。

    NOTE: 各ファンドについて target_filing_date 付近のスナップショットと
    その直前のスナップショットの差分から、買い増し/売りを判定する。
    該当データが無いファンドは、その時点の集計から除外する。

    戻り値: {
      "target_filing_date": ...,
      "participating_funds": 計算に参加できたファンドのリスト,
      "signals": [当時の一致シグナルのリスト],
    }
    """
    tracker: dict[str, dict] = {}
    participating_funds = []

    for fund in FUNDS:
        fund_id = fund["id"]
        current_snap, previous_snap = get_snapshot_and_previous(fund_id, target_filing_date)
        if not current_snap or not previous_snap:
            continue
        participating_funds.append(fund_id)

        diffs = compute_diff(current_snap.get("holdings", []), previous_snap.get("holdings", []))
        for d in diffs:
            cusip = d.get("cusip", "")
            delta = d.get("delta_usd", 0)
            if not cusip or delta == 0:
                continue
            entry = tracker.setdefault(cusip, {"name": d.get("name_of_issuer", "UNKNOWN"), "buy": set(), "sell": set()})
            if delta > 0:
                entry["buy"].add(fund_id)
            else:
                entry["sell"].add(fund_id)

    signals = []
    for cusip, entry in tracker.items():
        if len(entry["buy"]) >= 2 and len(entry["sell"]) == 0:
            direction, fund_ids = "buy", entry["buy"]
        elif len(entry["sell"]) >= 2 and len(entry["buy"]) == 0:
            direction, fund_ids = "sell", entry["sell"]
        else:
            continue
        signals.append({
            "cusip": cusip,
            "name": entry["name"].title(),
            "direction": direction,
            "fund_count": len(fund_ids),
            "fund_ids": sorted(fund_ids),
        })

    signals.sort(key=lambda s: (-s["fund_count"], s["cusip"]))
    return {
        "target_filing_date": target_filing_date,
        "participating_funds": participating_funds,
        "signals": signals[:top_n],
    }


# ===== Similarity Score（Step 2: ○/△/×判定 → スコア化 → 過去ケース比較） =====
# NOTE: Similarity Score は「投資判断」「将来予測」ではなく、「今回のファンド
# 行動パターンが、過去のどのケースとどの程度似ているか」を示す指標。
# 各判定項目（criteria）は必ずデータとして保持し、後から項目追加・重み変更・
# 高度化（特にholding patternの分類）が容易な構造にする。

def determine_signal_leader(fund_ids: list[str], cusip: str, first_appearance_map: dict) -> str | None:
    """
    シグナルに関与しているファンドのうち、この銘柄を最も早くから保有して
    いた（追跡履歴内で）ファンドを返す。current/historical どちらの
    シグナルにも同じロジックを適用できる共通関数。
    """
    dates = first_appearance_map.get(cusip, {})
    candidates = [(fid, dates[fid]) for fid in fund_ids if fid in dates]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1])
    return candidates[0][0]


def classify_holding_trend(fund_id: str, cusip: str, filing_date: str, max_distance_days: int = 45) -> str | None:
    """
    指定ファンドの、指定時点前後における保有額の推移を、簡易的な
    トレンド分類で返す（"accumulating" = 増加傾向 / "distributing" = 減少傾向 /
    None = 判定不可）。

    NOTE: これは将来拡張するholding pattern分類
    （Initial/Continuous/Accelerated accumulation, Re-entry, Distribution等）
    の現時点での簡易プロキシ。今は「直前→直後で増えたか減ったか」の
    二値判定にとどめ、将来的に分類を細分化する余地を残す。

    NOTE: filing_date は「完全一致」ではなく「最も近い日付」でマッチングする。
    ファンドごとに同じ四半期でも提出日が数日ズレることがあるため
    （例: ブラックロック8/13、バンガード8/14）、厳密な文字列一致だと
    本来は同じ四半期のはずのデータが見つからず誤ってN/A判定になってしまう。
    """
    history = get_fund_cusip_value_history(fund_id, cusip)
    if not history:
        return None

    def dist(h):
        d = _date_diff_days(filing_date, h["filing_date"])
        return abs(d) if d is not None else float("inf")

    idx = min(range(len(history)), key=lambda i: dist(history[i]))
    if dist(history[idx]) > max_distance_days or idx == 0:
        return None
    before = history[idx - 1]["value_usd"]
    after = history[idx]["value_usd"]
    if after > before:
        return "accumulating"
    elif after < before:
        return "distributing"
    return None


def compare_signals_breakdown(current: dict, historical: dict, first_appearance_map: dict) -> dict:
    """
    現在のシグナルと、過去のあるシグナルを5つの観点で比較し、
    ○（一致）/ △（部分一致）/ ×（不一致）/ N/A（判定不可）を判定する。
    各項目の判定理由（detail）も保持し、スコアをブラックボックスにしない。

    戻り値: {
      "criteria": [
        {"key": "leading_fund", "label": "Same leading fund", "verdict": "○"|"△"|"×"|"N/A", "detail": "..."},
        ... (5項目)
      ],
      "score_pct": 0-100の類似度スコア（N/A項目は分母から除外して計算）,
    }
    """
    criteria = []

    # 1. Same leading fund
    cur_leader = determine_signal_leader(current["fund_ids"], current["cusip"], first_appearance_map)
    hist_leader = determine_signal_leader(historical["fund_ids"], historical["cusip"], first_appearance_map)
    if cur_leader is None or hist_leader is None:
        criteria.append({"key": "leading_fund", "label": "Same leading fund", "verdict": "N/A", "detail": "先行ファンドを判定できませんでした。"})
    elif cur_leader == hist_leader:
        criteria.append({"key": "leading_fund", "label": "Same leading fund", "verdict": "○", "detail": f"{FUND_DISPLAY_NAME.get(cur_leader, cur_leader)} が両ケースで先行していました。"})
    else:
        criteria.append({"key": "leading_fund", "label": "Same leading fund", "verdict": "×", "detail": f"先行ファンドが異なります（今回: {cur_leader} / 過去: {hist_leader}）。"})

    # 2. Same sector
    cur_sector = SECTOR_MAP.get(current["cusip"])
    hist_sector = SECTOR_MAP.get(historical["cusip"])
    if cur_sector is None or hist_sector is None:
        criteria.append({"key": "sector", "label": "Same sector", "verdict": "N/A", "detail": "分野が未分類のため判定できませんでした。"})
    elif cur_sector == hist_sector:
        label = SECTOR_LABELS.get(cur_sector, {}).get("label", cur_sector)
        criteria.append({"key": "sector", "label": "Same sector", "verdict": "○", "detail": f"両ケースとも「{label}」分野でした。"})
    else:
        criteria.append({"key": "sector", "label": "Same sector", "verdict": "×", "detail": "分野が異なります。"})

    # 3. Similar fund participation（関与ファンド数の近さ）
    diff = abs(current["fund_count"] - historical["fund_count"])
    if diff == 0:
        criteria.append({"key": "participation", "label": "Similar fund participation", "verdict": "○", "detail": f"関与ファンド数が同じです（{current['fund_count']}社）。"})
    elif diff == 1:
        criteria.append({"key": "participation", "label": "Similar fund participation", "verdict": "△", "detail": f"関与ファンド数が近いです（今回{current['fund_count']}社 / 過去{historical['fund_count']}社）。"})
    else:
        criteria.append({"key": "participation", "label": "Similar fund participation", "verdict": "×", "detail": f"関与ファンド数が大きく異なります（今回{current['fund_count']}社 / 過去{historical['fund_count']}社）。"})

    # 4. Similar capital flow pattern（買い/売りの方向）
    if current["direction"] == historical["direction"]:
        flow_label = "買い（積み増し）" if current["direction"] == "buy" else "売り（縮小）"
        criteria.append({"key": "capital_flow", "label": "Similar capital flow pattern", "verdict": "○", "detail": f"両ケースとも{flow_label}方向でした。"})
    else:
        criteria.append({"key": "capital_flow", "label": "Similar capital flow pattern", "verdict": "×", "detail": "資金の方向（買い/売り）が異なります。"})

    # 5. Similar holding increase pattern（簡易プロキシ：先行ファンドの保有トレンド）
    cur_trend = classify_holding_trend(cur_leader, current["cusip"], current.get("filing_date", "")) if cur_leader else None
    hist_trend = classify_holding_trend(hist_leader, historical["cusip"], historical.get("target_filing_date", historical.get("filing_date", ""))) if hist_leader else None
    if cur_trend is None or hist_trend is None:
        criteria.append({"key": "holding_pattern", "label": "Similar holding increase pattern", "verdict": "N/A", "detail": "保有トレンドを判定できませんでした。"})
    elif cur_trend == hist_trend:
        criteria.append({"key": "holding_pattern", "label": "Similar holding increase pattern", "verdict": "○", "detail": f"両ケースとも「{cur_trend}」傾向でした。"})
    else:
        criteria.append({"key": "holding_pattern", "label": "Similar holding increase pattern", "verdict": "×", "detail": "保有トレンドの傾向が異なります。"})

    # スコア計算: ○=1.0, △=0.5, ×=0.0、N/Aは分母から除外
    verdict_points = {"○": 1.0, "△": 0.5, "×": 0.0}
    scored = [verdict_points[c["verdict"]] for c in criteria if c["verdict"] in verdict_points]
    score_pct = round((sum(scored) / len(scored)) * 100) if scored else None

    return {"criteria": criteria, "score_pct": score_pct}


def generate_historical_signal_pool(exclude_cusips: set[str] | None = None) -> list[dict]:
    """
    追跡履歴内の様々な四半期について compute_historical_consensus_signals() を
    実行し、過去に発生した一致シグナルを集めたプール（比較対象データ）を作る。

    NOTE: exclude_cusips に含まれる銘柄（=今回のシグナル自身の銘柄）は
    除外する。「同じ銘柄の過去」はAdoption Archiveで別途扱っており、
    Similarity Scoreでは「異なる銘柄でも似た行動パターンだったか」を見たい。
    """
    exclude_cusips = exclude_cusips or set()

    # 追跡対象の全ファンドの全filing_dateを集めて、比較対象の日付候補とする
    all_dates: set[str] = set()
    for fund in FUNDS:
        for snap in load_all_snapshots(fund["id"]):
            fd = snap.get("filing_date", "")
            if fd:
                all_dates.add(fd)

    pool = []
    for d in sorted(all_dates):
        result = compute_historical_consensus_signals(d)
        for s in result["signals"]:
            if s["cusip"] in exclude_cusips:
                continue
            pool.append({**s, "filing_date": d})
    return pool


def compute_aftermath_for_signal(signal_with_date: dict, max_distance_days: int = 45) -> dict:
    """
    指定の過去シグナルについて、その後数四半期で確認された変化を集計する。
    断定的な因果関係（「これが原因で発生した」）は主張せず、
    「その後、関連する変化が確認された」という事実の提示にとどめる。

    NOTE: 「成功した過去ケースだけを集める」設計を避けるため、ここでは
    outcome_label として「followed（その後の追随が観測された）」と
    「limited（その後の追随は観測されなかった）」を中立的に分類するのみで、
    どちらが「良い結果」かという価値判断は行わない。将来的に
    Similar positive cases / Similar but limited outcome cases として
    UI側で分けて見せる拡張がしやすいよう、この中立ラベルだけ用意しておく。

    戻り値: {
      "funds_increased_after": その後さらに保有を増やしたファンドの数,
      "new_entrants_after": その後新たに参入したファンドの数,
      "outcome_label": "followed" | "limited"  # 中立的な観測結果ラベル
    }
    """
    cusip = signal_with_date["cusip"]
    base_date = signal_with_date["filing_date"]

    funds_increased = 0
    new_entrants = 0

    for fund in FUNDS:
        fund_id = fund["id"]
        history = get_fund_cusip_value_history(fund_id, cusip)
        if not history:
            continue

        def dist(h):
            d = _date_diff_days(base_date, h["filing_date"])
            return abs(d) if d is not None else float("inf")

        idx = min(range(len(history)), key=lambda i: dist(history[i]))
        if dist(history[idx]) > max_distance_days:
            continue

        base_value = history[idx]["value_usd"]
        later_values = [h["value_usd"] for h in history[idx + 1: idx + 3]]  # 直後2四半期分
        if not later_values:
            continue

        if base_value == 0 and any(v > 0 for v in later_values):
            new_entrants += 1
        elif any(v > base_value for v in later_values):
            funds_increased += 1

    outcome_label = "followed" if (funds_increased > 0 or new_entrants > 0) else "limited"

    return {
        "funds_increased_after": funds_increased,
        "new_entrants_after": new_entrants,
        "outcome_label": outcome_label,
    }


def find_similar_historical_cases(current_signal: dict, first_appearance_map: dict, historical_pool: list[dict], top_n: int = 3, similarity_threshold: int = 50) -> dict:
    """
    現在のシグナルについて、過去の一致シグナルプールの中から似ているケースを
    探し、上位 top_n 件の詳細（Breakdown・スコア・その後の変化）に加えて、
    「類似度○%以上のケースが全体で何件あったか」という参照統計
    （Historical reference）も返す。

    これにより、単一の「最も似ている1件」だけでなく、「このパターン自体が
    過去データの中でどれくらい一般的か」を、ユーザーが判断できるようにする。
    「成功率」「予測精度」のような言葉は使わず、あくまで観測件数の提示にとどめる。

    TOP1だけを提示すると「都合の良いケースだけ選んでいるのでは」という
    印象を与えかねないため、複数件を並べてユーザー自身が比較・判断できる
    ようにする。

    NOTE: 同じ銘柄(cusip)が複数の過去四半期で候補になることがあるが、
    重複を避けるため銘柄ごとに最もスコアが高い時点のみを候補として残す。

    NOTE（パフォーマンス）: historical_pool は呼び出し側で1回だけ
    generate_historical_signal_pool() を実行した結果を渡すこと。
    この関数自体は重い全履歴スキャンを行わない（以前はシグナルごとに
    再計算していたため、一致シグナル数が多いと実行時間が線形に膨らんで
    いた。プールを共有することで、全履歴スキャンは1回だけになる）。

    戻り値: {
      "top_cases": [
        {
          "historical_signal": {cusip, name, direction, fund_count, fund_ids, filing_date},
          "breakdown": compare_signals_breakdownの結果,
          "aftermath": compute_aftermath_for_signalの結果,
        }, ...
      ]（スコアの高い順、最大 top_n 件）,
      "reference": {
        "similar_cases_found": 類似度がthreshold以上だった過去ケースの総数,
        "followed_count": そのうち、その後の追随・増加が観測された件数,
        "limited_count": そのうち、その後の大きな変化が観測されなかった件数,
        "threshold_pct": 「類似」と数える閾値（%）,
      }
    }
    """
    empty_reference = {"similar_cases_found": 0, "followed_count": 0, "limited_count": 0, "threshold_pct": similarity_threshold}

    # 渡されたプールから、今回のシグナル自身の銘柄だけを除外する（軽い操作）
    pool = [h for h in historical_pool if h["cusip"] != current_signal["cusip"]]
    if not pool:
        return {"top_cases": [], "reference": empty_reference}

    # 銘柄(cusip)ごとに最高スコアの1件だけを残す
    best_per_cusip: dict[str, tuple] = {}
    for hist in pool:
        breakdown = compare_signals_breakdown(current_signal, hist, first_appearance_map)
        if breakdown["score_pct"] is None:
            continue
        cusip = hist["cusip"]
        if cusip not in best_per_cusip or breakdown["score_pct"] > best_per_cusip[cusip][1]["score_pct"]:
            best_per_cusip[cusip] = (hist, breakdown)

    ranked = sorted(best_per_cusip.values(), key=lambda pair: -pair[1]["score_pct"])

    # 閾値以上の全候補について、その後の変化（aftermath）を計算しキャッシュする
    aftermath_cache: dict[int, dict] = {}
    above_threshold = [(hist, bd) for hist, bd in ranked if bd["score_pct"] >= similarity_threshold]
    followed_count = 0
    limited_count = 0
    for hist, bd in above_threshold:
        am = compute_aftermath_for_signal(hist)
        aftermath_cache[id(hist)] = am
        if am["outcome_label"] == "followed":
            followed_count += 1
        else:
            limited_count += 1

    reference = {
        "similar_cases_found": len(above_threshold),
        "followed_count": followed_count,
        "limited_count": limited_count,
        "threshold_pct": similarity_threshold,
    }

    top_cases = []
    for hist, breakdown in ranked[:top_n]:
        aftermath = aftermath_cache.get(id(hist)) or compute_aftermath_for_signal(hist)
        top_cases.append({
            "historical_signal": hist,
            "breakdown": breakdown,
            "aftermath": aftermath,
        })

    return {"top_cases": top_cases, "reference": reference}


def compute_consensus_signals(top_n: int = 45) -> dict:
    """
    data.json の各ファンドの buys_extended / sells_extended を突き合わせて、
    「何ファンドが同じ銘柄を同じ方向（買い or 売り）に動かしているか」を集計する。

    これは「複数巨頭一致シグナル」パネル用のデータで、固定の作り話ではなく
    実際の直近四半期の増減データに基づく、計算可能な指標。

    戻り値: {
      "signals": [一致ファンド数の多い順のリスト],
      "total_companies_tracked": 集計対象になった銘柄の総数（重複除去後）
    }

    NOTE: 4ファンドのうち has_quarter_comparison が false（初回データ取得直後）の
    ファンドは「買い増し/売り」の判定ができないため、この集計から除外される。

    NOTE: total_companies_tracked は固定の「45」ではなく、実際に各ファンドの
    buys_extended/sells_extended に出現した銘柄を重複除去した実数。
    サイト名の「45」とは独立した、誠実さを優先した実測値。
    """
    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # cusip -> { buy: set(fund_ids), sell: set(fund_ids), name, desc, values: {fund_id: value_str} }
    tracker: dict[str, dict] = {}

    for fund in data["funds"]:
        if not fund.get("has_quarter_comparison"):
            continue  # 比較不可のファンドはこの集計に参加させない（誠実さのため）

        fund_id = fund["id"]
        for h in fund.get("buys_extended", []):
            cusip = h.get("ticker", "")
            if not cusip:
                continue
            entry = tracker.setdefault(cusip, {"name": h["name"], "desc": h["desc"], "buy": set(), "sell": set(), "values": {}})
            entry["buy"].add(fund_id)
            entry["values"][fund_id] = h["value"]
        for h in fund.get("sells_extended", []):
            cusip = h.get("ticker", "")
            if not cusip:
                continue
            entry = tracker.setdefault(cusip, {"name": h["name"], "desc": h["desc"], "buy": set(), "sell": set(), "values": {}})
            entry["sell"].add(fund_id)
            entry["values"][fund_id] = h["value"]

    total_companies_tracked = len(tracker)

    signals = []
    for cusip, entry in tracker.items():
        # 「買い」「売り」どちらか一方にのみ複数ファンドが一致している場合のみシグナルとして採用
        # （買いと売りが両方ついている銘柄は、ファンド間で意見が割れているので「一致」ではない）
        if len(entry["buy"]) >= 2 and len(entry["sell"]) == 0:
            direction, fund_ids = "buy", entry["buy"]
        elif len(entry["sell"]) >= 2 and len(entry["buy"]) == 0:
            direction, fund_ids = "sell", entry["sell"]
        else:
            continue

        signals.append({
            "cusip": cusip,
            "name": entry["name"],
            "desc": entry["desc"],
            "direction": direction,
            "fund_count": len(fund_ids),
            "fund_ids": sorted(fund_ids),
            "fund_values": {fid: entry["values"][fid] for fid in fund_ids},
        })

    # 一致ファンド数が多い順 → 同数ならCUSIPで安定ソート
    signals.sort(key=lambda s: (-s["fund_count"], s["cusip"]))
    return {
        "signals": signals[:top_n],
        "total_companies_tracked": total_companies_tracked,
    }


def compute_aggregate_flows(top_n: int = 10) -> dict:
    """
    追跡中の全ファンドの buys_extended / sells_extended を、銘柄（CUSIP）単位で
    金額を合算する。これは「何ファンドが一致しているか」（compute_consensus_signals）
    とは異なる指標で、「ファンドの規模に関わらず、全体としてどの銘柄に
    最も大きな資金が動いたか」を見る。

    例: 1ファンドだけが超大口で買った銘柄も、ここでは上位に出てくる
    （consensus_signalsでは2ファンド以上の一致が必要なため出てこない）。

    NOTE: has_quarter_comparison が false のファンドは、正確な増減（delta）が
    計算できないため、この集計から除外する。

    戻り値: {
      "top_buys": [...], "top_sells": [...], "fund_count": 集計に使ったファンド数
    }
    """
    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # cusip -> { name, desc, total_usd, fund_ids: set() }
    tracker: dict[str, dict] = {}
    fund_count = 0

    for fund in data["funds"]:
        if not fund.get("has_quarter_comparison"):
            continue
        fund_count += 1
        fund_id = fund["id"]

        for h in fund.get("buys_extended", []) + fund.get("sells_extended", []):
            cusip = h.get("ticker", "")
            if not cusip:
                continue
            raw = h.get("amount_usd_raw")
            if raw is None:
                continue  # 旧データ（amount_usd_rawが無い）はスキップ
            entry = tracker.setdefault(cusip, {"name": h["name"], "desc": h["desc"], "total_usd": 0, "fund_ids": set()})
            entry["total_usd"] += raw
            entry["fund_ids"].add(fund_id)

    all_entries = list(tracker.values())
    buys = sorted([e for e in all_entries if e["total_usd"] > 0], key=lambda e: e["total_usd"], reverse=True)
    sells = sorted([e for e in all_entries if e["total_usd"] < 0], key=lambda e: e["total_usd"])

    def fmt(e):
        return {
            "name": e["name"],
            "desc": e["desc"],
            "value_label": to_jpy_label(abs(e["total_usd"])),
            "fund_count": len(e["fund_ids"]),
        }

    return {
        "top_buys": [fmt(e) for e in buys[:top_n]],
        "top_sells": [fmt(e) for e in sells[:top_n]],
        "fund_count": fund_count,
    }


def update_aggregate_flows_in_data_json():
    """compute_aggregate_flows() の結果を data.json のトップレベルに保存する。"""
    result = compute_aggregate_flows()

    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["aggregate_flows_computed"] = result
    data["_meta"]["last_updated"] = time.strftime("%Y-%m-%d")

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return result


def update_consensus_signals_in_data_json():
    """
    compute_consensus_signals() の結果を data.json のトップレベルに保存する。
    各シグナルには、Premium向けの Early Movement Detection（誰が最初に
    保有を始めたか、その先行者の過去の実績）の計算結果も付与する。
    """
    result = compute_consensus_signals()
    enriched_signals = compute_first_movers(result["signals"])

    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 各シグナルに、関与ファンドの最新提出日のうち最も新しいものを filing_date として付与。
    # （Similarity Score計算でのトレンド比較に必要）
    fund_last_filing_date = {f["id"]: f.get("last_filing_date", "") for f in data["funds"]}

    # 「Has this pattern happened before?」用の先行実績トラックレコードを付与。
    # 同じファンドが複数シグナルの先行者になることがあるため、重複計算を避ける。
    first_appearance_map = build_global_first_appearance_map()

    # Similarity Score用の過去シグナルプールは、全シグナルで共有するため
    # ここで1回だけ計算する（以前はシグナルごとに再計算し、実行時間が
    # シグナル数に比例して膨らんでいた）。
    historical_pool = generate_historical_signal_pool()

    track_record_cache: dict[str, dict] = {}
    for s in enriched_signals:
        dates = [fund_last_filing_date.get(fid, "") for fid in s["fund_ids"] if fund_last_filing_date.get(fid)]
        s["filing_date"] = max(dates) if dates else ""

        fm = s.get("first_mover")
        if fm:
            fid = fm["fund_id"]
            if fid not in track_record_cache:
                track_record_cache[fid] = compute_fund_leadership_track_record(fid, first_appearance_map)
            fm["leader_track_record"] = track_record_cache[fid]

        # Similar Event Archive: この銘柄に過去どのファンドがどの順で参入したか
        s["adoption_archive"] = compute_company_adoption_archive(s["cusip"], first_appearance_map)

        # Similarity Score: 今回のシグナルと最も似ている過去ケース(TOP3)＋参照統計
        similarity_result = find_similar_historical_cases(s, first_appearance_map, historical_pool, top_n=3)
        s["similar_historical_cases"] = similarity_result["top_cases"]
        s["similarity_reference"] = similarity_result["reference"]

    data["consensus_signals_computed"] = enriched_signals
    data["consensus_signals_total_tracked"] = result["total_companies_tracked"]

    # Fund History: 全ファンドの行動履歴プロフィールを計算・保存
    update_fund_history_profiles_in_data_json(first_appearance_map, data)

    data["_meta"]["last_updated"] = time.strftime("%Y-%m-%d")

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return {"signals": enriched_signals, "total_companies_tracked": result["total_companies_tracked"]}


def update_data_json(fund_id: str, result: dict, filing_date: str, trend: dict | None = None, pattern_similarity: dict | None = None):
    """
    data.json の該当ファンドの buys/sells/buys_extended/sells_extended/trend/pattern_similarity を更新する。

    NOTE: fund_id が data.json にまだ存在しない場合（新しくFUNDSに追加したファンドの
    初回実行時など）は、最小限のフィールドを持つ新規ファンドオブジェクトを自動生成して
    data["funds"] に追加する。これにより、新ファンド追加時に data.json を手動編集する
    必要がない（index.html 側の表示用フィールド who/intro/timeline は空のまま保存され、
    UIは後述のフォールバック処理で「データ準備中」として扱う）。
    """
    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    fund_name = next((f["name"] for f in FUNDS if f["id"] == fund_id), fund_id)

    target_fund = None
    for fund in data["funds"]:
        if fund["id"] == fund_id:
            target_fund = fund
            break

    if target_fund is None:
        # 新規ファンド: 最小限のスタブを作成して追加
        # NEW_FUND_TIMELINES に定義済みの場合は、調査済みの実話エピソードを使う
        preset = NEW_FUND_TIMELINES.get(fund_id)
        target_fund = {
            "id": fund_id,
            "name": fund_name,
            "cik": next((f["cik"] for f in FUNDS if f["id"] == fund_id), ""),
            "who": preset["who"] if preset else f"— {fund_name} / SEC 13F-HR開示ベース",
            "intro": preset["intro"] if preset else f"<strong>{fund_name}の個別の動き。</strong>過去の出来事との関連は今後追加されます。",
            "timeline": preset["timeline"] if preset else [],
            "buys": [],
            "sells": [],
        }
        data["funds"].append(target_fund)
        print(f"  ※ data.json に新規ファンド「{fund_name}」のエントリを作成しました。"
              f"{'（調査済みの歴史エピソードを含む）' if preset else ''}")

    fund = target_fund
    if result["buys"]:
        fund["buys"] = result["buys"]
    if result["sells"]:
        fund["sells"] = result["sells"]
    # 拡張リストは空でも上書きする（「データが無い」状態を正しく反映するため）
    fund["buys_extended"] = result["buys_extended"]
    fund["sells_extended"] = result["sells_extended"]
    fund["last_filing_date"] = filing_date
    fund["has_quarter_comparison"] = result["has_comparison"]
    if trend is not None:
        fund["trend"] = trend
    fund["pattern_similarity"] = pattern_similarity  # Noneの場合も明示的に保存（データなし状態を反映）

    data["_meta"]["last_updated"] = time.strftime("%Y-%m-%d")

    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    print(f"=== THE 45 PROJECT: SEC EDGAR 13F-HR 自動取得 ===")
    print(f"User-Agent: {USER_AGENT}")
    print(f"対象ファンド: {[f['id'] for f in FUNDS]}")
    print()

    if "contact@example.com" in USER_AGENT:
        print("⚠️  警告: SEC_USER_AGENT が未設定です。実際の連絡先メールアドレスを設定してください。")
        print("    例: export SEC_USER_AGENT='THE45Project youremail@example.com'")
        print()

    for fund in FUNDS:
        fund_id = fund["id"]
        cik = fund["cik"]
        print(f"--- {fund['name']} (CIK: {cik}) ---")

        try:
            latest = get_latest_13f_accession(cik)
            time.sleep(REQUEST_DELAY_SEC)

            if not latest:
                print(f"  13F-HR が見つかりませんでした。スキップします。")
                continue

            filing_date = latest["filing_date"]
            print(f"  最新13F-HR: accession={latest['accession']} filed={filing_date}")

            xml_text = fetch_information_table_xml(cik, latest["accession"])
            time.sleep(REQUEST_DELAY_SEC)

            holdings = parse_holdings(xml_text, filing_date=filing_date)
            print(f"  保有銘柄数: {len(holdings)}")

            if not holdings:
                print(f"  保有データが空でした。スキップします。")
                continue

            holdings_agg = aggregate_by_cusip(holdings)

            # 既にこの filing_date のスナップショットが保存済みなら、
            # 今回の実行が「同じ四半期の再実行」である可能性が高い。
            # その場合は「1つ前」のスナップショットと比較する（後述のload_previous_snapshotが処理）。
            previous = load_previous_snapshot(fund_id, filing_date)
            previous_holdings = previous["holdings"] if previous else None

            if previous_holdings:
                print(f"  前回スナップショット: {previous['filing_date']} と比較します。")
            else:
                print(f"  ⚠️ 前回スナップショットが見つかりません。今回が初回データとして保存されます。")
                print(f"     （次回実行時から「買い/売り」の本当の判定が始まります）")

            result = build_fund_holdings(holdings_agg, previous_holdings, top_n=TOP_N_DEFAULT, extended_n=EXTENDED_N_DEFAULT)
            has_comparison = result["has_comparison"]
            buys, sells = result["buys"], result["sells"]

            label = "増加上位" if has_comparison else "保有額上位（初回・比較不可）"
            print(f"  {label}{len(buys)}銘柄:")
            for b in buys:
                print(f"    - {b['name']} ({b['ticker']}): {b['value']}")

            if sells:
                print(f"  減少上位{len(sells)}銘柄:")
                for s in sells:
                    print(f"    - {s['name']} ({s['ticker']}): {s['value']}")

            print(f"  拡張リスト: buys_extended={len(result['buys_extended'])}件, sells_extended={len(result['sells_extended'])}件")

            # トレンド計算: スナップショットを保存する前に、保存後の状態を見越して計算する
            # （save_snapshotの後に計算すれば「今回分」も含めたトレンドになる）
            save_snapshot(fund_id, filing_date, holdings_agg)
            trend = compute_fund_trend(fund_id)

            if trend["has_enough_history"]:
                print(f"  トレンド: {len(trend['quarters'])}四半期分のデータが揃いました。")
                for q in trend["quarters"]:
                    print(f"    - {q['filing_date']}: 合計{q['total_value_label']} / {q['position_count']}銘柄 / 上位5集中度{q['top5_concentration_pct']}%")
            else:
                print(f"  トレンド: まだ{len(trend['quarters'])}四半期分のみ。2四半期以上集まると表示開始されます。")

            # 過去の参照局面との類似度計算
            pattern_similarity = None
            try:
                pattern_similarity = compute_pattern_similarity(fund_id)
                if pattern_similarity:
                    ev = pattern_similarity["most_similar_event"]
                    print(f"  類似度: 現在の集中度{pattern_similarity['current_concentration_pct']}% は「{ev['event_label']}」"
                          f"（当時{ev['before_concentration_pct']}%）と{pattern_similarity['similarity_pct']}%類似")
                    if ev["top_buy"]:
                        print(f"    当時の実際の買い: {ev['top_buy']['name']} (+{ev['top_buy']['value_label']})")
                    if ev["top_sell"]:
                        print(f"    当時の実際の売り: {ev['top_sell']['name']} (-{ev['top_sell']['value_label']})")
                else:
                    print(f"  類似度: 参照局面のデータが不足しているため計算できませんでした。")
            except Exception as e:
                print(f"  ❌ 類似度計算でエラー: {e}")

            update_data_json(fund_id, result, filing_date, trend=trend, pattern_similarity=pattern_similarity)
            print(f"  data.json を更新し、スナップショットを保存しました。")

        except Exception as e:
            print(f"  ❌ エラー: {e}")
            continue

        print()

    # ===== クロスファンド集計: 複数巨頭一致シグナル =====
    print("--- 複数巨頭一致シグナルを集計中 ---")
    try:
        result = update_consensus_signals_in_data_json()
        signals = result["signals"]
        total_tracked = result["total_companies_tracked"]
        print(f"  追跡対象の銘柄総数: {total_tracked}社")
        if signals:
            pct = round(len(signals) / total_tracked * 100, 1) if total_tracked else 0
            print(f"  {len(signals)}件の一致シグナルを検出しました（全{total_tracked}社中 {pct}%）:")
            for s in signals[:10]:
                direction_label = "買い" if s["direction"] == "buy" else "売り"
                fm = s.get("first_mover")
                fm_note = f" / 最速: {fm['fund_id']}({fm['since']}〜)" if fm else ""
                print(f"    - {s['name']}: {s['fund_count']}ファンド一致（{direction_label}） [{', '.join(s['fund_ids'])}]{fm_note}")
        else:
            print("  一致シグナルは検出されませんでした（比較可能なファンドが2社未満、または一致なし）。")
    except Exception as e:
        print(f"  ❌ 一致シグナル集計でエラー: {e}")

    print()
    print("--- Fund History（ファンド別の行動履歴プロフィール）---")
    try:
        with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
            current_data = json.load(f)
        for f_entry in current_data["funds"]:
            profile = f_entry.get("history_profile")
            if not profile:
                continue
            sectors_note = ", ".join(f"{SECTOR_LABELS.get(sid, {}).get('label', sid)}:{c}件" for sid, c in profile.get("sector_lead_counts", {}).items())
            lag_note = f" / 平均{profile['avg_lag_days']}日後に追随" if profile.get("avg_lag_days") else ""
            sector_part = f" / 先行分野: {sectors_note}" if sectors_note else ""
            print(f"  {f_entry['name']}: 先行{profile['led_count']}件 / 追随{profile['followed_count']}件{lag_note}{sector_part}")
    except Exception as e:
        print(f"  ❌ Fund History集計でエラー: {e}")

    # ===== クロスファンド集計: 45社全体の資金フロー =====
    print()
    print("--- 全ファンド合算の資金フローを集計中 ---")
    try:
        flow_result = update_aggregate_flows_in_data_json()
        print(f"  集計対象ファンド数: {flow_result['fund_count']}")
        print(f"  合算 買い上位:")
        for b in flow_result["top_buys"][:5]:
            print(f"    - {b['name']}: +約{b['value_label']}（{b['fund_count']}ファンドが関与）")
        print(f"  合算 売り上位:")
        for s in flow_result["top_sells"][:5]:
            print(f"    - {s['name']}: -約{s['value_label']}（{s['fund_count']}ファンドが関与）")
    except Exception as e:
        print(f"  ❌ 資金フロー集計でエラー: {e}")

    # ===== クロスファンド集計: セクター別の傾向 =====
    print()
    print("--- セクター別の傾向を集計中 ---")
    try:
        sector_result = update_sector_breakdown_in_data_json()
        print(f"  分類済み銘柄数: {sector_result['total_classified']} / 未分類: {sector_result['unclassified_count']}")
        for s in sector_result["sectors"]:
            print(f"    - {s['label']}: {s['company_count']}社（買い{s['buy_count']}件・売り{s['sell_count']}件、買い比率{s['buy_pct']}%）")
    except Exception as e:
        print(f"  ❌ セクター集計でエラー: {e}")

    print()
    print("=== 完了 ===")
    print()
    print("【データの見方】")
    print("・ has_quarter_comparison が true のファンドは、前四半期との比較に基づく")
    print("  本当の「買い増し/売り」が data.json に反映されています。")
    print("・ false の場合は、前回データがまだ無いため保有額ランキングで代用しています。")
    print("  次回の四半期提出後、自動的に本当の比較に切り替わります。")


if __name__ == "__main__":
    main()
