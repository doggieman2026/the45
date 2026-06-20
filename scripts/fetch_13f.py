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
]

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
                diffs.append({
                    "name_of_issuer": name,
                    "cusip": cusip,
                    "value_usd": 0,
                    "shares": 0,
                    "delta_usd": -prev_value,
                    "is_exited_position": True,
                })

    return diffs


def build_fund_holdings(
    holdings: list[dict],
    previous_holdings: list[dict] | None,
    top_n: int = TOP_N_DEFAULT,
) -> tuple[list[dict], list[dict], bool]:
    """
    保有銘柄リストから「買い（増加上位）」「売り（減少上位）」を構築する。

    previous_holdings が None の場合（初回実行・履歴なし）は、
    比較ができないため "保有額が大きい銘柄" を buys の代理として返し、
    sells は空にする。戻り値の3番目 (has_comparison) が False になる。
    """
    holdings = aggregate_by_cusip(holdings)

    if previous_holdings is None:
        # 初回実行: 比較不可。保有額ベースの簡易表示にフォールバック。
        sorted_holdings = sorted(holdings, key=lambda h: h.get("value_usd", 0), reverse=True)
        top = sorted_holdings[:top_n]
        max_value = top[0]["value_usd"] if top else 1
        buys = [_format_holding_row(h, max_value, is_increase=True) for h in top]
        return buys, [], False

    diffs = compute_diff(holdings, previous_holdings)

    increases = sorted([d for d in diffs if d["delta_usd"] > 0], key=lambda d: d["delta_usd"], reverse=True)
    decreases = sorted([d for d in diffs if d["delta_usd"] < 0], key=lambda d: d["delta_usd"])

    top_buys = increases[:top_n]
    top_sells = decreases[:top_n]

    max_buy = top_buys[0]["delta_usd"] if top_buys else 1
    max_sell = abs(top_sells[0]["delta_usd"]) if top_sells else 1

    buys = [_format_holding_row(h, max_buy, is_increase=True, use_delta=True) for h in top_buys]
    sells = [_format_holding_row(h, max_sell, is_increase=False, use_delta=True) for h in top_sells]

    return buys, sells, True


def _format_holding_row(h: dict, max_value: int, is_increase: bool, use_delta: bool = False) -> dict:
    """保有/増減データ1件を、サイト表示用のdict形式に整形する。"""
    cusip = h.get("cusip", "")
    name_raw = h.get("name_of_issuer", "UNKNOWN")
    override = TICKER_OVERRIDES.get(cusip, None)
    display_name = override["name"] if override else name_raw.title()
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
    }


def update_data_json(fund_id: str, buys: list[dict], sells: list[dict], filing_date: str, has_comparison: bool):
    """data.json の該当ファンドの buys/sells を更新する。"""
    with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    for fund in data["funds"]:
        if fund["id"] == fund_id:
            if buys:
                fund["buys"] = buys
            if sells:
                fund["sells"] = sells
            fund["last_filing_date"] = filing_date
            fund["has_quarter_comparison"] = has_comparison
            break

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

            buys, sells, has_comparison = build_fund_holdings(holdings_agg, previous_holdings, top_n=TOP_N_DEFAULT)

            label = "増加上位" if has_comparison else "保有額上位（初回・比較不可）"
            print(f"  {label}{len(buys)}銘柄:")
            for b in buys:
                print(f"    - {b['name']} ({b['ticker']}): {b['value']}")

            if sells:
                print(f"  減少上位{len(sells)}銘柄:")
                for s in sells:
                    print(f"    - {s['name']} ({s['ticker']}): {s['value']}")

            update_data_json(fund_id, buys, sells, filing_date, has_comparison)
            save_snapshot(fund_id, filing_date, holdings_agg)
            print(f"  data.json を更新し、スナップショットを保存しました。")

        except Exception as e:
            print(f"  ❌ エラー: {e}")
            continue

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
