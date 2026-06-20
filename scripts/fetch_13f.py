#!/usr/bin/env python3
"""
THE 45 PROJECT — SEC EDGAR 13F-HR 自動取得スクリプト
=====================================================

概要:
  data.sec.gov の無料・APIキー不要のRESTful APIを使って、
  主要ファンドの最新13F-HR（四半期保有報告書）から保有銘柄データを取得し、
  data.json の "funds[].buys" "funds[].sells" を自動生成・更新する。

実行方法:
  python3 fetch_13f.py

前提:
  - SEC EDGARのルール上、すべてのリクエストに連絡先メールアドレスを含む
    User-Agent ヘッダーが必須。 SEC_USER_AGENT 環境変数で設定すること。
    例: export SEC_USER_AGENT="THE45Project contact@example.com"
  - レート制限: 10 requests/sec を超えないこと（このスクリプトは安全マージンを取って実装）
  - GitHub Actions上で実行する想定（ネットワークアクセスが必要）

注意:
  - 13F-HRはCUSIPでしか銘柄を識別しないため、CUSIP→ティッカー変換が必要。
    これは company_tickers.json だけでは不十分なため、簡易マッピングテーブル
    (TICKER_OVERRIDES) を併用する。本格運用では CUSIP マスタの整備を推奨。
  - 出力する日本円換算額はその時点のドル円レートで概算するため、
    厳密な金額ではなく「規模感」を示す目的の表示であることに留意。
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
REQUEST_DELAY_SEC = 0.15  # 10 req/sec制限に対する安全マージン

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


def parse_holdings(xml_text: str) -> list[dict]:
    """
    13F Information Table XML をパースして保有銘柄リストを返す。
    各 infoTable には nameOfIssuer, cusip, value (千ドル単位), shrsOrPrnAmt が含まれる。
    """
    # 名前空間がファイルにより異なることがあるため、タグ名のローカル部分でマッチさせる
    ns_strip = re.sub(r'xmlns(:\w+)?="[^"]+"', "", xml_text)
    root = ET.fromstring(ns_strip)

    holdings = []
    for info_table in root.iter():
        if info_table.tag.split("}")[-1] != "infoTable":
            continue
        row = {}
        for child in info_table:
            tag = child.tag.split("}")[-1]
            if tag == "nameOfIssuer":
                row["name_of_issuer"] = child.text
            elif tag == "cusip":
                row["cusip"] = child.text
            elif tag == "value":
                # 13Fのvalueは「千ドル」単位
                try:
                    row["value_usd"] = int(child.text) * 1000
                except (TypeError, ValueError):
                    row["value_usd"] = 0
            elif tag == "shrsOrPrnAmt":
                for sub in child:
                    if sub.tag.split("}")[-1] == "sshPrnamt":
                        try:
                            row["shares"] = int(sub.text)
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


def build_fund_holdings(holdings: list[dict], top_n: int = 5) -> tuple[list[dict], list[dict]]:
    """
    保有銘柄リストから「買い（上位）」「売り（下位/縮小）」を構築する。
    NOTE: 本来は「前期比の増減」が必要。今回のスニペットは保有額の大きい順を
    'buys' の代理として扱う簡易版。本格運用では前四半期データとの差分を取ること。
    """
    sorted_holdings = sorted(holdings, key=lambda h: h.get("value_usd", 0), reverse=True)
    top = sorted_holdings[:top_n]

    max_value = top[0]["value_usd"] if top else 1

    buys = []
    for h in top:
        cusip = h.get("cusip", "")
        name_raw = h.get("name_of_issuer", "UNKNOWN")
        override = TICKER_OVERRIDES.get(cusip, None)  # CUSIPベースの上書きは将来拡張用
        display_name = override["name"] if override else name_raw.title()
        desc = override["desc"] if override else "SEC 13F-HR 開示銘柄"
        bar = round((h.get("value_usd", 0) / max_value) * 100) if max_value else 0
        buys.append({
            "name": display_name,
            "ticker": cusip,  # CUSIP→ティッカー変換は別途整備が必要
            "desc": desc,
            "value": f"+約{to_jpy_label(h.get('value_usd', 0))}",
            "bar": max(bar, 5),
        })

    # 売りは「前期比較データ」が無いと正確に出せないため、空配列で返す。
    # TODO: 前四半期のholdingsをキャッシュし、減少額の大きい銘柄をsellsとして構築する。
    sells = []

    return buys, sells


def update_data_json(fund_id: str, buys: list[dict], sells: list[dict], filing_date: str):
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

            print(f"  最新13F-HR: accession={latest['accession']} filed={latest['filing_date']}")

            xml_text = fetch_information_table_xml(cik, latest["accession"])
            time.sleep(REQUEST_DELAY_SEC)

            holdings = parse_holdings(xml_text)
            print(f"  保有銘柄数: {len(holdings)}")

            if not holdings:
                print(f"  保有データが空でした。スキップします。")
                continue

            buys, sells = build_fund_holdings(holdings, top_n=5)
            print(f"  上位{len(buys)}銘柄を取得:")
            for b in buys:
                print(f"    - {b['name']} ({b['ticker']}): {b['value']}")

            update_data_json(fund_id, buys, sells, latest["filing_date"])
            print(f"  data.json を更新しました。")

        except Exception as e:
            print(f"  ❌ エラー: {e}")
            continue

        print()

    print("=== 完了 ===")
    print()
    print("【重要】このスクリプトは「上位保有額ランキング」を 'buys' として出力します。")
    print("実際の「新規購入・買い増し」を判定するには、前四半期との比較が必要です。")
    print("次のステップ: 前期データをキャッシュして増減差分を計算する処理を追加してください。")


if __name__ == "__main__":
    main()
