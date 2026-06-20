#!/usr/bin/env python3
"""
THE 45 PROJECT — 過去13F-HR バックフィルスクリプト
=====================================================

概要:
  fetch_13f.py の通常実行は「最新1件」だけを取得するが、このスクリプトは
  SEC EDGARに残っている「過去すべての13F-HR提出」を遡って取得し、
  data/history/{fund_id}/{filing_date}.json にスナップショットとして保存する。

  これにより：
  - 四半期トレンド機能（compute_fund_trend）が、何ヶ月も待たずに
    最初から複数年分のグラフを表示できるようになる
  - 類似度計算機能の「過去の参照局面」（2007年リーマン前、2020年コロナ前等）に
    使う実データを、今すぐ用意できる

実行方法:
  python3 backfill_history.py
  （引数なしで全ファンドを対象に実行。一度だけ実行すればよい）

  特定のファンドだけ実行する場合:
  python3 backfill_history.py --fund blackrock

前提:
  - fetch_13f.py と同じディレクトリに置くこと（関数を再利用するため）
  - SEC_USER_AGENT 環境変数が必須（fetch_13f.py と同条件）
  - 実行に時間がかかる（ファンドあたり提出件数 × 2リクエスト + ページネーション分）。
    レート制限を守るため、間に十分なスリープを入れている。
  - 既にスナップショットが存在する filing_date はスキップする
    （何度実行しても安全・差分のみ取得する設計）

注意:
  - これは「一度だけ実行する」一括処理用のスクリプトであり、
    GitHub Actions の月次自動実行（update-13f-data.yml）には含めない。
    手動で workflow_dispatch するか、ローカルで一度実行することを想定。
  - SEC EDGARが提供する範囲（だいたい1999年以降）より古いデータは取得できない。
  - 提出件数が多いファンド（ブラックロック等）はファイル数が多く、
    実行に数分かかることがある。
"""

import sys
import time
from pathlib import Path

# fetch_13f.py の関数・定数を再利用
sys.path.insert(0, str(Path(__file__).parent))
from fetch_13f import (
    FUNDS,
    REQUEST_DELAY_SEC,
    SNAPSHOT_DIR,
    get_all_13f_accessions,
    fetch_information_table_xml,
    parse_holdings,
    aggregate_by_cusip,
    save_snapshot,
    snapshot_path,
    USER_AGENT,
)


def backfill_fund(fund_id: str, cik: str, fund_name: str):
    print(f"--- {fund_name} (CIK: {cik}) ---")

    try:
        all_filings = get_all_13f_accessions(cik)
    except Exception as e:
        print(f"  ❌ 提出履歴の取得に失敗: {e}")
        return

    print(f"  過去の13F-HR提出件数: {len(all_filings)}件 "
          f"（{all_filings[0]['filing_date'] if all_filings else '-'} 〜 "
          f"{all_filings[-1]['filing_date'] if all_filings else '-'}）")

    fetched = 0
    skipped = 0
    failed = 0

    for filing in all_filings:
        filing_date = filing["filing_date"]
        existing_path = snapshot_path(fund_id, filing_date)

        if existing_path.exists():
            skipped += 1
            continue

        try:
            time.sleep(REQUEST_DELAY_SEC)
            xml_text = fetch_information_table_xml(cik, filing["accession"])
            time.sleep(REQUEST_DELAY_SEC)

            holdings = parse_holdings(xml_text, filing_date=filing_date)
            if not holdings:
                print(f"    {filing_date}: 保有データが空。スキップ。")
                failed += 1
                continue

            holdings_agg = aggregate_by_cusip(holdings)
            save_snapshot(fund_id, filing_date, holdings_agg)
            fetched += 1
            print(f"    {filing_date}: {len(holdings_agg)}銘柄を保存しました。")

        except Exception as e:
            print(f"    {filing_date}: ❌ エラー: {e}")
            failed += 1
            continue

    print(f"  完了: 新規取得={fetched}件 / 既存スキップ={skipped}件 / 失敗={failed}件")
    print()


def main():
    print("=== THE 45 PROJECT: 過去13F-HR バックフィル ===")
    print(f"User-Agent: {USER_AGENT}")
    print("対象ファンド:", [f["id"] for f in FUNDS])
    print()
    print("⚠️  これは時間のかかる処理です。ファンドごとに提出件数分のリクエストを行います。")
    print()

    target_fund_id = None
    if "--fund" in sys.argv:
        idx = sys.argv.index("--fund")
        if idx + 1 < len(sys.argv):
            target_fund_id = sys.argv[idx + 1]

    for fund in FUNDS:
        if target_fund_id and fund["id"] != target_fund_id:
            continue
        backfill_fund(fund["id"], fund["cik"], fund["name"])

    print("=== 完了 ===")
    print("次に fetch_13f.py を実行すれば、今回取得した過去スナップショットを使って")
    print("正しい四半期トレンドが data.json に反映されます。")


if __name__ == "__main__":
    main()
