#!/usr/bin/env python3
"""境川橋の水位を取得し、CSVに蓄積、警戒判定を行い、必要なら通知する。"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from chart import build_svg_chart
from fetch import FetchError, Reading, fetch_camera_snapshot, fetch_rain, fetch_station, load_config
from forecast import RainContext, clean_readings, judge, latest_valid_reading, should_run_full_check
from notify import build_failure_message, build_message, send_slack
from status import build_status_markdown

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LEVELS_DIR = DATA_DIR / "levels"
RAIN_DIR = DATA_DIR / "rain"
LEGACY_LEVELS_PATH = DATA_DIR / "levels.csv"
STATE_PATH = BASE_DIR / "state.json"
CONFIG_PATH = BASE_DIR / "config.toml"
ASSETS_DIR = BASE_DIR / "assets"
STATUS_PATH = BASE_DIR / "STATUS.md"

LEVEL_CSV_FIELDS = ["observed_at", "value", "suspect"]
RAIN_CSV_FIELDS = ["observed_at", "precipitation_mm"]


def month_path(directory: Path, dt: datetime) -> Path:
    return directory / f"{dt:%Y-%m}.csv"


def _read_month_csv(path: Path, key_field: str) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row[key_field]] = row
    return rows


def _write_month_csv(path: Path, fields: list[str], rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for key in sorted(rows.keys()):
            writer.writerow(rows[key])


def merge_and_save_rows(directory: Path, fields: list[str], key_field: str, rows: list[dict[str, str]]) -> bool:
    """行を観測時刻の属する月ごとのファイル（directory/YYYY-MM.csv）にマージする。

    月をまたぐ行が混在していても、それぞれの月のファイルに正しく振り分ける。
    データは削除しない（将来のモデル検証に使う蓄積が目的のため）。
    """
    changed = False
    by_month: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        dt = datetime.fromisoformat(row[key_field])
        by_month.setdefault(f"{dt:%Y-%m}", []).append(row)

    for ym, month_rows in by_month.items():
        path = directory / f"{ym}.csv"
        existing = _read_month_csv(path, key_field)
        for row in month_rows:
            key = row[key_field]
            if key not in existing:
                changed = True
            existing[key] = row
        _write_month_csv(path, fields, existing)
    return changed


def migrate_legacy_levels_csv() -> None:
    """旧`data/levels.csv`（単一ファイル）を月別ファイルへ一度だけ移行する。"""
    if not LEGACY_LEVELS_PATH.exists():
        return
    existing = _read_month_csv(LEGACY_LEVELS_PATH, "observed_at")
    if existing:
        merge_and_save_rows(LEVELS_DIR, LEVEL_CSV_FIELDS, "observed_at", list(existing.values()))
    LEGACY_LEVELS_PATH.unlink()


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"notified": {}, "consecutive_failures": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def merge_and_save_levels(readings: list[Reading], suspects: dict[datetime, bool]) -> bool:
    """観測値を月別CSV（data/levels/YYYY-MM.csv）にマージする。変化があれば True。"""
    rows = [
        {
            "observed_at": r.observed_at.isoformat(),
            "value": str(r.value),
            "suspect": str(suspects.get(r.observed_at, False)),
        }
        for r in readings
    ]
    return merge_and_save_rows(LEVELS_DIR, LEVEL_CSV_FIELDS, "observed_at", rows)


def merge_and_save_rain(points, now: datetime) -> None:
    """実測分（now以前）の降雨のみ月別CSV（data/rain/YYYY-MM.csv）に蓄積する。

    予報値（未来分）は実測ではないため保存しない。
    """
    rows = [
        {"observed_at": p.time.isoformat(), "precipitation_mm": str(p.precipitation_mm)}
        for p in points
        if p.time <= now
    ]
    if rows:
        merge_and_save_rows(RAIN_DIR, RAIN_CSV_FIELDS, "observed_at", rows)


def get_latest_known_value() -> float | None:
    """CSVに残っている最新の観測値（前回までの本実行で取得したもの）。

    当月ファイルが空（月初でまだ本実行がない等）の場合は前月ファイルも見る。
    """
    now = datetime.now()
    for dt in (now, (now.replace(day=1) - timedelta(days=1))):
        existing = _read_month_csv(month_path(LEVELS_DIR, dt), "observed_at")
        if existing:
            latest_key = max(existing.keys())
            return float(existing[latest_key]["value"])
    return None


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


def update_status_page(cleaned, judgement, thresholds, now, generated_at, rain, cfg) -> None:
    """STATUS.md・水位グラフ・(該当時のみ)河川カメラ画像を書き出す。

    予測パス（judgement.forecast_schedule）は judge() が計算したものをそのまま描画する。
    Slack通知のETAと同じ元データを使うため、グラフと通知で予測が食い違わない。

    カメラ画像は常に「表示される枚数 == 保存されているファイル数」を保つため、
    書き込み前に既存のcamera_*.jpgを必ず削除してから、必要な場合のみ1枚だけ保存する。
    """
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    chart_result = build_svg_chart(
        cleaned,
        thresholds,
        now,
        judgement.forecast_schedule,
        window_hours=cfg["chart"]["window_hours"],
        forecast_hours=cfg["chart"]["forecast_hours"],
    )
    (ASSETS_DIR / "chart.svg").write_text(chart_result.svg, encoding="utf-8")

    for old in ASSETS_DIR.glob("camera_*.jpg"):
        old.unlink()

    camera_relpath = None
    camera_captured_at = None
    threshold = cfg["camera"]["embed_threshold"]
    if judgement.current_value is not None and judgement.current_value >= threshold:
        snapshot = fetch_camera_snapshot(cfg["camera"]["camera_id"])
        if snapshot is not None:
            filename = f"camera_{snapshot.captured_at:%Y%m%d_%H%M%S}.jpg"
            (ASSETS_DIR / filename).write_bytes(snapshot.image_bytes)
            camera_relpath = f"assets/{filename}"
            camera_captured_at = snapshot.captured_at

    rain_total_mm = sum(p.precipitation_mm for p in rain) if rain else None

    status_md = build_status_markdown(
        judgement,
        cleaned,
        thresholds,
        now,
        generated_at,
        "assets/chart.svg",
        camera_relpath,
        camera_captured_at,
        rain_total_mm,
        cfg,
    )
    STATUS_PATH.write_text(status_md, encoding="utf-8")


def run(dry_run: bool, force: bool = False) -> int:
    migrate_legacy_levels_csv()

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
    changed = merge_and_save_levels(snapshot.readings, suspects)

    thresholds = {**cfg["thresholds"], **snapshot.thresholds}
    if snapshot.thresholds and snapshot.thresholds != {k: cfg["thresholds"][k] for k in snapshot.thresholds}:
        print("警告: ページ上の基準水位が config.toml と異なります。基準改定の可能性があります。", file=sys.stderr)

    rain_series = fetch_rain(cfg["rain"]["latitude"], cfg["rain"]["longitude"])
    rain_context = None
    rain_for_display = None
    if rain_series is not None:
        merge_and_save_rain(rain_series.points, now)
        anchor_reading = latest_valid_reading(cleaned)
        if anchor_reading is not None:
            window_minutes = cfg["forecast"]["trend_window_minutes"]
            window_start = anchor_reading.observed_at - timedelta(minutes=window_minutes)
            recent_mm = sum(
                p.precipitation_mm
                for p in rain_series.points
                if window_start <= p.time <= anchor_reading.observed_at
            )
            future_buckets = [
                ((p.time - anchor_reading.observed_at).total_seconds() / 60.0, p.precipitation_mm)
                for p in rain_series.points
                if p.time > anchor_reading.observed_at
            ]
            rain_context = RainContext(recent_mm=recent_mm, future_buckets=future_buckets)
        display_horizon = now + timedelta(hours=cfg["chart"]["forecast_hours"])
        rain_for_display = [p for p in rain_series.points if now < p.time <= display_horizon]

    j = judge(cleaned, now, thresholds, cfg, rain=rain_context)

    exceed_notify = levels_to_notify(j, thresholds, state, now, cfg)
    approach_notify = approaching_to_notify(j, state, now, cfg)
    newly_notified = list(dict.fromkeys(exceed_notify + approach_notify))

    update_status_page(cleaned, j, thresholds, now, snapshot.generated_at, rain_for_display, cfg)

    message = build_message(j, newly_notified, rain_for_display, cfg)
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
