#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
THE45 PROJECT — compute_observed_changes.py（修正版 v2）
=====================================================

概要:
  data/history/{fund_id}/*.json のスナップショット群（source of truth）から、
  各主体の observed_changes を完全に再計算する。

v2での重要な変更（GitHubのファイルサイズ上限対応）:
  observed_changes は funds.json には保存しない。
  代わりに data/observed_changes/{fund_id}.json という、スナップショットと
  同じ「主体ごとの別ファイル」に保存する。

  理由: BlackRock・Citadelのように保有銘柄数千件規模の主体は、
  数四半期分の比較だけでも変化イベントが大量に生成される。これを
  全45主体分まとめて funds.json（1つの巨大ファイル）に書き込むと、
  ファイルサイズがGitHubの上限（100MB）を容易に超えてしまう。

  スナップショットと同じ「1主体1ファイル」の分割方式にすることで、
  この問題を構造的に回避する。funds.json はマスタ情報（静的な主体情報 +
  quarters という軽量な四半期サマリー）だけを持つ、常に小さいファイルのまま維持される。

生成するもの（変更なし・固定）:
  - type: "new" | "increased" | "decreased" | "exited" の4種類のみ
  - 各変化の金額・株数・変化率（事実の数値）
  - span: 同方向の変化が何四半期連続しているかという「カウントのみ」

生成しないもの（変更なし・意図的に含めない）:
  - 重要度スコア・注目度ランキング
  - 複数主体間の一致判定
  - 解釈文言・断定的な評価
"""

import json
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).parent.parent / "data" / "history"
OBSERVED_CHANGES_DIR = Path(__file__).parent.parent / "data" / "observed_changes"
FUNDS_JSON_PATH = Path(__file__).parent.parent / "funds.json"


# ===== スナップショット読み込み（fetch_funds.py と共通の考え方） =====

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


def filing_date_to_quarter_label(filing_date: str) -> str:
    year, month, _ = filing_date.split("-")
    month = int(month)
    q = (month - 1) // 3 + 1
    return f"{year}-Q{q}"


# ===== 銘柄ごとの時系列を組み立てる =====

def _build_cusip_timeseries(snapshots: list[dict]) -> dict[str, list[dict]]:
    """
    全スナップショットから、銘柄(CUSIP)ごとの時系列を組み立てる。

    戻り値: {
      cusip: [
        {"filing_date": ..., "value_usd": ..., "shares": ...,
         "name_of_issuer": ..., "title_of_class": ...},
        ...  # 保有していない四半期も value_usd=0 として明示的に含める
      ],
      ...
    }
    保有していない四半期を明示的に0として含めることで、
    「new」（0→正の値）「exited」（正の値→0）の判定を一貫した方法で行える。
    """
    all_cusips: set[str] = set()
    per_quarter_holdings: list[dict[str, dict]] = []  # 四半期ごとの {cusip: holding}

    for snap in snapshots:
        quarter_map = {}
        for h in snap.get("holdings", []):
            cusip = h.get("cusip", "")
            if not cusip:
                continue
            quarter_map[cusip] = h
            all_cusips.add(cusip)
        per_quarter_holdings.append(quarter_map)

    timeseries: dict[str, list[dict]] = {cusip: [] for cusip in all_cusips}

    for snap, quarter_map in zip(snapshots, per_quarter_holdings):
        filing_date = snap.get("filing_date", "")
        for cusip in all_cusips:
            h = quarter_map.get(cusip)
            if h:
                timeseries[cusip].append({
                    "filing_date": filing_date,
                    "value_usd": h.get("value_usd", 0),
                    "shares": h.get("shares", 0),
                    "name_of_issuer": h.get("name_of_issuer", "UNKNOWN"),
                    "title_of_class": h.get("title_of_class", ""),
                })
            else:
                timeseries[cusip].append({
                    "filing_date": filing_date,
                    "value_usd": 0,
                    "shares": 0,
                    "name_of_issuer": None,
                    "title_of_class": None,
                })

    return timeseries


# ===== 1銘柄の時系列から、変化イベントを抽出する =====

def _classify_transition(prev_value: int, curr_value: int) -> str | None:
    """2つの四半期間の変化を4種類のいずれかに分類する。変化が無ければNone。"""
    if prev_value == 0 and curr_value > 0:
        return "new"
    if prev_value > 0 and curr_value == 0:
        return "exited"
    if curr_value > prev_value:
        return "increased"
    if curr_value < prev_value:
        return "decreased"
    return None  # 変化なし


def _compute_span(series: list[dict], end_index: int, direction: str) -> dict:
    """
    end_index時点の変化（direction: 'increased' or 'decreased'）が、
    何四半期連続しているかを過去に遡ってカウントする。

    NOTE: 事実のカウントのみ。「連続◯四半期＝重要」という判断はしない。
    NOTE: end_indexの変化そのものは既に direction と分かっているので count=1 から開始し、
    ループでは「その1つ前の遷移」から遡って確認する（同じ遷移を二重に数えない）。
    """
    count = 1
    earliest_transition_end = end_index

    while earliest_transition_end - 1 >= 1:
        candidate_end = earliest_transition_end - 1
        prev_v = series[candidate_end - 1]["value_usd"]
        curr_v = series[candidate_end]["value_usd"]
        # _classify_transition と同じ判定基準を使う。ここが独自の単純比較
        # （curr>prev なら increased 等）だと、new（0→正の値）や
        # exited（正の値→0）まで「増加/減少の連続」として遡ってしまう
        # バグになるため、必ず _classify_transition() を通す。
        d = _classify_transition(prev_v, curr_v)
        if d != direction:
            break
        count += 1
        earliest_transition_end = candidate_end

    start_index = earliest_transition_end - 1  # この連続変化が始まる直前の四半期
    return {
        "first_quarter_observed": filing_date_to_quarter_label(series[start_index]["filing_date"]),
        "consecutive_quarters": count,
    }


def compute_observed_changes_for_fund(fund_id: str) -> list[dict]:
    """
    1主体分のobserved_changesを、スナップショット全件から完全に再計算する。

    戻り値は filing_date 昇順の変化イベントのリスト。
    各要素:
    {
      "filing_date": "2025-11-14",
      "type": "increased",              # new / increased / decreased / exited のみ
      "security_name": "...",
      "cusip": "...",
      "prior_value_usd": 12300000000,
      "current_value_usd": 15800000000,
      "delta_usd": 3500000000,
      "delta_pct": 28.5,
      "span": {                          # increased / decreased のみ付与
        "first_quarter_observed": "2025-Q1",
        "consecutive_quarters": 3
      }
    }
    """
    snapshots = _load_fund_snapshots(fund_id)
    if len(snapshots) < 2:
        return []  # 比較対象が無ければ変化は生成しない

    timeseries = _build_cusip_timeseries(snapshots)
    changes: list[dict] = []

    for cusip, series in timeseries.items():
        for i in range(1, len(series)):
            prev_v = series[i - 1]["value_usd"]
            curr_v = series[i]["value_usd"]
            change_type = _classify_transition(prev_v, curr_v)
            if change_type is None:
                continue

            # security_name は在庫がある四半期の名称を優先して使う
            # （exitedの場合は直前の四半期の名称を使う）
            name_source = series[i] if series[i]["name_of_issuer"] else series[i - 1]
            security_name = (name_source.get("name_of_issuer") or "UNKNOWN").title()

            delta_usd = curr_v - prev_v
            delta_pct = round((delta_usd / prev_v) * 100, 1) if prev_v else None

            event = {
                "filing_date": series[i]["filing_date"],
                "type": change_type,
                "security_name": security_name,
                "cusip": cusip,
                "prior_value_usd": prev_v,
                "current_value_usd": curr_v,
                "delta_usd": delta_usd,
                "delta_pct": delta_pct,
            }

            # span は increased / decreased のみに付与する。
            # new は「保有を開始した」という事実そのものが記録であり、
            # exited は「保有をやめた」という事実そのものが記録であるため、
            # どちらも「連続性」という概念になじまない。
            if change_type in ("increased", "decreased"):
                event["span"] = _compute_span(series, i, change_type)

            changes.append(event)

    changes.sort(key=lambda e: e["filing_date"])
    return changes


# ===== 保存（主体ごとの別ファイル） =====

def save_observed_changes(fund_id: str, changes: list[dict]) -> None:
    """data/observed_changes/{fund_id}.json へ保存する。
    スナップショット（data/history/{fund_id}/*.json）と同じ考え方で、
    主体ごとに独立したファイルにする。funds.jsonには埋め込まない。
    """
    OBSERVED_CHANGES_DIR.mkdir(parents=True, exist_ok=True)
    path = OBSERVED_CHANGES_DIR / f"{fund_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fund_id": fund_id, "changes": changes}, f, ensure_ascii=False, indent=2)


def load_observed_changes(fund_id: str) -> list[dict]:
    """data/observed_changes/{fund_id}.json から読み込む。無ければ空配列。"""
    path = OBSERVED_CHANGES_DIR / f"{fund_id}.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f).get("changes", [])


# ===== funds.json への反映 =====
# NOTE: funds.json には observed_changes の配列そのものは保存しない。
# 代わりに、件数（事実の数）とファイルパスへの参照だけを軽量に持たせる。
# これにより funds.json は常に小さいまま維持される
# （保有銘柄数千件規模の主体が混ざっていてもファイルサイズが膨らまない）。

def update_observed_changes(fund_ids: list[str] | None = None) -> None:
    """
    指定主体（省略時は全45主体）について、observed_changesを
    スナップショットから完全再計算し、data/observed_changes/{fund_id}.json に保存する。
    funds.json側には、件数と参照パスだけを軽量に反映する。
    """
    with open(FUNDS_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    targets = fund_ids or [f["id"] for f in data["funds"]]

    for fund in data["funds"]:
        if fund["id"] not in targets:
            continue

        changes = compute_observed_changes_for_fund(fund["id"])
        save_observed_changes(fund["id"], changes)

        # funds.json 側は、件数と参照パスのみ（事実の数のみで、解釈は含まない）
        fund["observed_changes_summary"] = {
            "path": f"data/observed_changes/{fund['id']}.json",
            "total_count": len(changes),
            "new_count": sum(1 for c in changes if c["type"] == "new"),
            "exited_count": sum(1 for c in changes if c["type"] == "exited"),
        }

        print(f"{fund['display_name_ja']}: {len(changes)}件の変化を "
              f"data/observed_changes/{fund['id']}.json に保存")

    with open(FUNDS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import sys
    ids = sys.argv[1:] if len(sys.argv) > 1 else None
    update_observed_changes(ids)
