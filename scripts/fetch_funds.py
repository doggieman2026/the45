#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
THE45 PROJECT — fetch_funds.py（設計版・45 CAPITAL MOVERS 対応 / v2）
=====================================================

概要:
  既存 scripts/fetch_13f.py の「SEC EDGAR取得・XML解析・スナップショット保存」
  部分をそのまま再利用しつつ、データの入出力先を旧 data.json（ファンド単位の
  buys/sells構造）から funds.json（45 CAPITAL MOVERS マスタ）に切り替える。

  この段階での責務は「45主体から安定してデータを取得できる基盤」のみ。
  buys/sells判定・pattern_similarity・consensus_signals・sector_breakdownは
  意図的に含めない（次フェーズ：observed_changes生成で改めて設計する）。

v2での変更点:
  1. エラー処理: 1主体の失敗が全体を止めない設計を明確化。
     自動取得の成否は data_quality_notes（人間が書く構造的な注意点）とは
     別フィールド last_fetch_status に記録する。成功した主体は保存し、
     失敗した主体は次回再取得可能なまま残す。
  2. Source of truth: funds.json の quarters / data_coverage は、
     data/history/{fund_id}/*.json のスナップショット群から毎回
     「ゼロから再計算」する（差分追記ではない）。スナップショットが正、
     funds.jsonのquartersはそこから導出される表示用キャッシュという位置づけ。

既存コードとの関係:
  以下の関数は fetch_13f.py から変更なしで再利用する（fund非依存の汎用部品）：
    - http_get()
    - get_latest_13f_accession()
    - fetch_information_table_xml()
    - parse_holdings()
    - aggregate_by_cusip()
    - snapshot_path()
    - save_snapshot()

  以下は新規（このファイルで定義）：
    - load_funds() / save_funds()             : funds.json の読み書き
    - rebuild_quarters_from_snapshots()        : スナップショット群からquarters/data_coverageを完全再計算
    - fetch_one_fund()                         : 1主体分の取得（成功/失敗を明示的に返す）
    - main()                                   : 45主体をループして実行、失敗しても継続

実行方法:
  python3 fetch_funds.py                  # 全45主体を対象に実行
  python3 fetch_funds.py --id berkshire   # 特定の1主体のみ実行（動作確認用）
  python3 fetch_funds.py --rebuild-only   # SEC取得を行わず、既存スナップショットからquartersだけ再計算

前提:
  - SEC_USER_AGENT 環境変数が必須（fetch_13f.py と同条件）
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ===== 既存 fetch_13f.py から変更なしで再利用する部分 =====
# scripts/fetch_13f.py と同じディレクトリに配置すること（この import が解決できる必要がある）。
from fetch_13f import (
    http_get,
    get_latest_13f_accession,
    fetch_information_table_xml,
    parse_holdings,
    aggregate_by_cusip,
    snapshot_path,
    save_snapshot,
    load_all_snapshots,
    USER_AGENT,
)

REQUEST_DELAY_SEC = 0.15  # SEC 10 req/sec制限への安全マージン（fetch_13f.pyと同値）

FUNDS_JSON_PATH = Path(__file__).parent.parent / "funds.json"
SNAPSHOT_DIR = Path(__file__).parent.parent / "data" / "history"


# ===== funds.json 読み書き =====

def load_funds() -> dict:
    """funds.json を読み込む。存在しない場合は明示的にエラーとする
    （companies.jsonと違い、funds.jsonは事前にマスタとして用意されている前提のため、
    自動生成のフォールバックは持たせない）。
    """
    if not FUNDS_JSON_PATH.exists():
        raise FileNotFoundError(
            f"funds.json が見つかりません: {FUNDS_JSON_PATH}\n"
            f"45主体のマスタ情報が先に用意されている必要があります。"
        )
    with open(FUNDS_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_funds(data: dict) -> None:
    """funds.json を書き戻す。_meta.total_fundsとfunds配列の実件数が
    一致しているかを保存前に検証する（人為的な欠落を早期に検知するため）。
    """
    actual_count = len(data.get("funds", []))
    expected_count = data.get("_meta", {}).get("total_funds")
    if expected_count is not None and actual_count != expected_count:
        raise ValueError(
            f"funds.json の件数不整合: _meta.total_funds={expected_count} "
            f"だが実際の funds 配列は {actual_count} 件。保存を中止します。"
        )
    with open(FUNDS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find_fund_record(data: dict, fund_id: str) -> dict | None:
    for fund in data["funds"]:
        if fund["id"] == fund_id:
            return fund
    return None


# ===== quarters / data_coverage の計算（スナップショットが正） =====
# NOTE: funds.json の quarters / data_coverage は、data/history/{fund_id}/ 配下の
# 全スナップショットから「毎回ゼロから再計算」する。差分追記ではない。
# 理由: スナップショット(JSON生データ)を唯一の正とし、funds.json側はそこから
# 導出される表示用キャッシュに位置づけることで、funds.json側が万一壊れても
# 一次データから常に再生成できる。FILE生成や将来の別表示形式からも、
# このスナップショット群を直接参照できる。

def _load_fund_snapshots(fund_id: str) -> list[dict]:
    """data/history/{fund_id}/*.json を filing_date 昇順で全て読み込む。"""
    fund_dir = SNAPSHOT_DIR / fund_id
    if not fund_dir.exists():
        return []
    snapshots = []
    for p in sorted(fund_dir.glob("*.json"), key=lambda x: x.stem):
        with open(p, "r", encoding="utf-8") as f:
            snapshots.append(json.load(f))
    return snapshots


def _quarter_summary_from_holdings(holdings: list[dict], filing_date: str) -> dict:
    """1四半期分の保有データから、事実の集計値のみを作る（評価は含まない）。"""
    total_value = sum(h.get("value_usd", 0) for h in holdings)
    position_count = len(holdings)
    sorted_h = sorted(holdings, key=lambda h: h.get("value_usd", 0), reverse=True)
    top5_value = sum(h.get("value_usd", 0) for h in sorted_h[:5])
    top5_pct = round((top5_value / total_value) * 100, 1) if total_value else 0.0

    return {
        "filing_date": filing_date,
        "total_value_usd": total_value,
        "position_count": position_count,
        "top5_concentration_pct": top5_pct,
    }


def filing_date_to_quarter_label(filing_date: str) -> str:
    """'2026-03-31' のような期末日を '2026-Q1' 形式に変換する。"""
    year, month, _ = filing_date.split("-")
    month = int(month)
    q = (month - 1) // 3 + 1
    return f"{year}-Q{q}"


def rebuild_quarters_from_snapshots(fund_id: str) -> tuple[list[dict], dict]:
    """
    スナップショット群（source of truth）から、funds.json用の
    quarters配列とdata_coverageを完全に再計算する。

    戻り値: (quarters, data_coverage)
    """
    snapshots = _load_fund_snapshots(fund_id)

    if not snapshots:
        return [], {
            "first_available_quarter": None,
            "latest_available_quarter": None,
            "quarter_count": 0,
            "backfilled": False,
        }

    quarters = [
        _quarter_summary_from_holdings(snap.get("holdings", []), snap.get("filing_date", ""))
        for snap in snapshots
    ]

    quarter_labels = [filing_date_to_quarter_label(q["filing_date"]) for q in quarters]

    data_coverage = {
        "first_available_quarter": quarter_labels[0],
        "latest_available_quarter": quarter_labels[-1],
        "quarter_count": len(quarter_labels),
        # backfilled: 5四半期以上の連続データがあるかという事実ベースの目安。評価ではない。
        "backfilled": len(quarter_labels) >= 5,
    }

    return quarters, data_coverage


# ===== メイン処理（1主体分） =====

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_one_fund(fund: dict) -> dict:
    """
    1主体分の最新13F-HRを取得し、スナップショット保存まで行う。

    戻り値は必ず dict（例外を送出しない）。成否は status フィールドで判定する：
      status="success"  : 新規取得・保存に成功
      status="skipped"  : 既に当該filing_dateのスナップショットが存在
      status="no_filing": 13F-HRが見つからない
      status="error"    : 取得・パース中に例外が発生

    この関数は失敗時も例外を投げない設計にしている。呼び出し側（main）で
    1主体の失敗が他の44主体の処理を止めないようにするため。
    """
    fund_id = fund["id"]
    cik = fund["cik"]
    print(f"--- {fund['display_name_ja']} (id={fund_id}, CIK={cik}) ---")

    try:
        latest = get_latest_13f_accession(cik)
        time.sleep(REQUEST_DELAY_SEC)

        if not latest:
            print(f"  13F-HR が見つかりませんでした。")
            return {"fund_id": fund_id, "status": "no_filing", "checked_at": _now_iso()}

        filing_date = latest["filing_date"]

        existing_path = snapshot_path(fund_id, filing_date)
        if existing_path.exists():
            print(f"  {filing_date} は取得済みです。スキップします。")
            return {"fund_id": fund_id, "status": "skipped", "filing_date": filing_date, "checked_at": _now_iso()}

        xml_text = fetch_information_table_xml(cik, latest["accession"])
        time.sleep(REQUEST_DELAY_SEC)

        holdings = parse_holdings(xml_text, filing_date=filing_date)
        if not holdings:
            print(f"  保有データが空でした。")
            return {"fund_id": fund_id, "status": "error", "error": "empty holdings", "checked_at": _now_iso()}

        holdings_agg = aggregate_by_cusip(holdings)
        save_snapshot(fund_id, filing_date, holdings_agg)
        print(f"  {filing_date}: {len(holdings_agg)}銘柄を保存しました。")

        return {
            "fund_id": fund_id,
            "status": "success",
            "filing_date": filing_date,
            "checked_at": _now_iso(),
        }

    except Exception as e:
        # ここで捕捉することで、Loews（合算提出）・SoftBank系（関連法人が複数）・
        # CalPERS（直近提出未確認）のような、法人構造が複雑な主体や一時的な
        # SEC側エラーが発生しても、残り44主体の取得は継続される。
        print(f"  ❌ エラー: {e}")
        return {"fund_id": fund_id, "status": "error", "error": str(e), "checked_at": _now_iso()}


def update_fund_record(data: dict, result: dict) -> None:
    """
    fetch_one_fund() の結果を funds.json の該当レコードに反映する。

    - last_fetch_status: 自動取得の成否ログ。人間が書く data_quality_notes とは
      別フィールドにすることで、「構造上の注意点（人間が管理）」と
      「直近の取得が成功したか（機械が管理）」を混在させない。
    - quarters / data_coverage: statusが success の場合のみ、
      rebuild_quarters_from_snapshots() でスナップショットから完全再計算する
      （差分追記ではない）。
    """
    fund = find_fund_record(data, result["fund_id"])
    if fund is None:
        print(f"  ⚠️ funds.json に {result['fund_id']} のレコードが見つかりません。")
        return

    fund["last_fetch_status"] = {
        "status": result["status"],
        "checked_at": result["checked_at"],
        "error": result.get("error"),
    }

    if result["status"] == "success":
        fund["last_verified_filing_date"] = result["filing_date"]
        quarters, coverage = rebuild_quarters_from_snapshots(result["fund_id"])
        fund["quarters"] = quarters
        fund["data_coverage"] = coverage


# ===== エントリポイント =====

def main():
    parser = argparse.ArgumentParser(description="THE45: 45主体のSEC EDGAR 13F-HR取得")
    parser.add_argument("--id", help="特定のfund idのみ実行（動作確認用）", default=None)
    parser.add_argument(
        "--rebuild-only", action="store_true",
        help="SEC取得を行わず、既存スナップショットからquarters/data_coverageのみ再計算する"
    )
    args = parser.parse_args()

    print("=== THE45 PROJECT: funds.json 連携 13F-HR取得 ===")
    data = load_funds()
    target_funds = data["funds"]

    if args.id:
        target_funds = [f for f in target_funds if f["id"] == args.id]
        if not target_funds:
            print(f"指定されたid '{args.id}' はfunds.jsonに存在しません。")
            sys.exit(1)

    print(f"対象: {len(target_funds)}主体" + ("（rebuild-onlyモード）" if args.rebuild_only else ""))
    print()

    status_counts = {"success": 0, "skipped": 0, "no_filing": 0, "error": 0}

    for fund in target_funds:
        if args.rebuild_only:
            # SEC取得を行わず、既存スナップショットから quarters/data_coverage だけ再計算する。
            # funds.jsonのquarters配列が万一破損・削除された場合の復旧手段として使う。
            quarters, coverage = rebuild_quarters_from_snapshots(fund["id"])
            fund["quarters"] = quarters
            fund["data_coverage"] = coverage
            print(f"--- {fund['display_name_ja']} (id={fund['id']}) --- "
                  f"再計算: {coverage['quarter_count']}四半期分")
            continue

        # fetch_one_fund は例外を投げない設計なので、ここでの try/except は
        # 「万一の想定外」に対する最終防衛線として残す。
        try:
            result = fetch_one_fund(fund)
        except Exception as e:
            result = {"fund_id": fund["id"], "status": "error", "error": str(e), "checked_at": _now_iso()}

        update_fund_record(data, result)
        status_counts[result["status"]] = status_counts.get(result["status"], 0) + 1

        # 1主体ごとに保存する。45主体分を通しで実行する間にどこかで
        # 中断しても、それまでの成功分が失われないようにするため。
        save_funds(data)
        print()

    if not args.rebuild_only:
        print("=== 完了 ===")
        print(f"成功: {status_counts['success']}件 / スキップ: {status_counts['skipped']}件 / "
              f"提出なし: {status_counts['no_filing']}件 / 失敗: {status_counts['error']}件")
        if status_counts["error"] > 0:
            print("失敗した主体は funds.json の last_fetch_status に記録されています。"
                  "次回実行時に自動的に再取得が試みられます（GitHub Actionsの月次実行、または手動再実行）。")
    else:
        save_funds(data)
        print("=== rebuild完了 ===")


if __name__ == "__main__":
    main()
