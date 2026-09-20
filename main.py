#!/usr/bin/env python3
"""境川橋の水位を取得し、CSVに蓄積、警戒判定を行い、必要なら通知する。"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fetch import FetchError, Reading, fetch_rain_forecast, fetch_station, load_config
from forecast import LEVEL_ORDER, clean_readings, judge, should_run_full_check
from notify import build_failure_message, build_message, send_slack

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "data" / "levels.csv"
STATE_PATH = BASE_DIR / "state.json"
CONFIG_PATH = BASE_DIR / "config.toml"

CSV_FIELDS = ["observed_at", "value", "suspect"]


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"notified": {}, "consecutive_failures": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_existing_csv() -> dict[str, tuple[float, bool]]:
    rows: dict[str, tuple[float, bool]] = {}
    if DATA_PATH.exists():
        with DATA_PATH.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["observed_at"]] = (float(row["value"]), row["suspect"] == "True")
    return rows


def merge_and_save_csv(readings: list[Reading], suspects: dict[datetime, bool]) -> bool:
    """既存CSVに新規観測値をマージする。変化があれば True を返す。"""
    existing = load_existing_csv()
    changed = False
    for r in readings:
        key = r.observed_at.isoformat()
        suspect = suspects.get(r.observed_at, False)
        if key not in existing:
            changed = True
        existing[key] = (r.value, suspect)

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DATA_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for key in sorted(existing.keys()):
            value, suspect = existing[key]
            writer.writerow({"observed_at": key, "value": value, "suspect": suspect})
    return changed


def get_latest_known_value() -> float | None:
    """CSVに残っている最新の観測値（前回までの本実行で取得したもの）。"""
    existing = load_existing_csv()
    if not existing:
        return None
    latest_key = max(existing.keys())
    return existing[latest_key][0]


def levels_to_notify(judgement, thresholds: dict[str, float], state: dict, now: datetime, cfg: dict) -> list[str]:
    """ヒステリシス・クールダウンを考慮し、今回新たに通知すべき段階を返す。"""
    notified = state.setdefault("notified", {})
    margin = cfg["forecast"]["hysteresis_margin"]
    cooldown = cfg["forecast"]["cooldown_minutes"]
    to_notify: list[str] = []

    for level in judgement.exceeded_levels:
        threshold = thresholds[level]
        entry = notified.get(level)
        if entry:
            last_time = datetime.fromisoformat(entry["at"])
            elapsed_min = (now - last_time).total_seconds() / 60.0
            if elapsed_min < cooldown:
                continue
        if judgement.current_value is not None and judgement.current_value < threshold - margin:
            continue
        to_notify.append(level)
        notified[level] = {"at": now.isoformat()}

    # 超えなくなった段階の記録は消し、再超過時に確実に再通知されるようにする
    for level in list(notified.keys()):
        threshold = thresholds.get(level)
        if threshold is not None and judgement.current_value is not None and judgement.current_value < threshold - margin:
            del notified[level]

    return to_notify


def approaching_to_notify(judgement, state: dict, now: datetime, cfg: dict) -> list[str]:
    notified = state.setdefault("notified_approaching", {})
    cooldown = cfg["forecast"]["cooldown_minutes"]
    to_notify: list[str] = []
    active_levels = {e.level for e in judgement.approaching}

    for eta in judgement.approaching:
        entry = notified.get(eta.level)
        if entry:
            elapsed_min = (now - datetime.fromisoformat(entry["at"])).total_seconds() / 60.0
            if elapsed_min < cooldown:
                continue
        to_notify.append(eta.level)
        notified[eta.level] = {"at": now.isoformat()}

    for level in list(notified.keys()):
        if level not in active_levels:
            del notified[level]

    return to_notify


def run(dry_run: bool, force: bool = False) -> int:
    cfg = load_config(str(CONFIG_PATH))
    state = load_state()
    now = datetime.now()

    if not force:
        last_value = get_latest_known_value()
        last_checked_at = (
            datetime.fromisoformat(state["last_checked_at"]) if state.get("last_checked_at") else None
        )
        if not should_run_full_check(last_value, last_checked_at, now, cfg):
            print(
                f"平常時のため今回はスキップ（前回本実行: {last_checked_at}, 直近水位: {last_value}）"
            )
            return 0

    state["last_checked_at"] = now.isoformat()

    try:
        snapshot = fetch_station(cfg["station"]["url"])
    except FetchError as exc:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        threshold = cfg["failure"]["consecutive_failures_to_notify"]
        if state["consecutive_failures"] >= threshold:
            msg = build_failure_message(str(exc), cfg)
            print(msg)
            if not dry_run:
                webhook = os.environ.get("SLACK_WEBHOOK_URL")
                if webhook:
                    send_slack(webhook, msg)
        save_state(state)
        return 1

    state["consecutive_failures"] = 0

    cleaned = clean_readings(snapshot.readings, cfg["forecast"]["max_jump_per_10min"])
    suspects = {c.observed_at: c.suspect for c in cleaned}
    changed = merge_and_save_csv(snapshot.readings, suspects)

    thresholds = {**cfg["thresholds"], **snapshot.thresholds}
    if snapshot.thresholds and snapshot.thresholds != {k: cfg["thresholds"][k] for k in snapshot.thresholds}:
        print("警告: ページ上の基準水位が config.toml と異なります。基準改定の可能性があります。", file=sys.stderr)

    j = judge(cleaned, now, thresholds, cfg)

    exceed_notify = levels_to_notify(j, thresholds, state, now, cfg)
    approach_notify = approaching_to_notify(j, state, now, cfg)
    newly_notified = list(dict.fromkeys(exceed_notify + approach_notify))

    rain = fetch_rain_forecast(cfg["rain"]["latitude"], cfg["rain"]["longitude"])

    message = build_message(j, newly_notified, rain, cfg)
    if message:
        print(message)
        if not dry_run:
            webhook = os.environ.get("SLACK_WEBHOOK_URL")
            if webhook:
                send_slack(webhook, message)
    else:
        print(f"通知なし。現在水位: {j.current_value}")

    save_state(state)

    if not dry_run and changed:
        print("CSV更新あり（呼び出し元でcommitしてください）")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="境川橋の水位監視")
    parser.add_argument("--dry-run", action="store_true", help="Slackへ送信せず標準出力のみ")
    parser.add_argument("--force", action="store_true", help="平常時の間引きを無視して必ず本実行する")
    args = parser.parse_args()
    return run(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
