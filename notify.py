"""Slack通知の整形と送信。"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from urllib.error import URLError

from fetch import RainForecastPoint
from forecast import LEVEL_LABELS, Judgement


def format_minutes(m: float) -> str:
    m = max(m, 0)
    if m < 60:
        return f"{round(m)}分"
    h = int(m // 60)
    rem = round(m % 60)
    return f"{h}時間{rem}分" if rem else f"{h}時間"


def build_message(
    judgement: Judgement,
    newly_notified_levels: list[str],
    rain: list[RainForecastPoint] | None,
    cfg: dict,
) -> str | None:
    """通知すべきことが何もなければ None を返す（呼び出し側は送信しない）。"""
    if judgement.no_data:
        return None
    if not newly_notified_levels and not judgement.surge_10min and not judgement.surge_30min:
        return None

    lines = []
    header_level = judgement.current_level
    if header_level and judgement.current_level in newly_notified_levels:
        lines.append(f"⚠️ 境川橋: {LEVEL_LABELS[header_level]}（{judgement.current_value:.2f}m）を超過しました")
    elif judgement.surge_10min or judgement.surge_30min:
        lines.append(f"⚠️ 境川橋: 水位が急上昇しています（現在 {judgement.current_value:.2f}m）")
    else:
        lines.append(f"⚠️ 境川橋: 水位 {judgement.current_value:.2f}m")

    for eta in judgement.approaching:
        if eta.level in newly_notified_levels:
            lines.append(
                f"・{LEVEL_LABELS[eta.level]}まで約 {format_minutes(eta.eta_minutes_low)}〜{format_minutes(eta.eta_minutes_high)}"
            )

    if judgement.surge_10min:
        lines.append("・直近10分で急上昇（+0.15m以上）")
    if judgement.surge_30min:
        lines.append("・直近30分で急上昇（+0.3m以上）")

    if rain:
        total = sum(p.precipitation_mm for p in rain)
        if total > 0:
            lines.append(f"・今後3時間の予測降水量: 約{total:.1f}mm（藤沢市周辺）")

    lines.append(f"詳細: {cfg['notify']['kanagawa_page']}")
    lines.append(f"カメラ映像: {cfg['notify']['camera_page']}")
    lines.append(cfg["notify"]["disclaimer"])
    return "\n".join(lines)


def build_failure_message(error_summary: str, cfg: dict) -> str:
    return (
        "🛑 sakaigawa-watch: 境川橋の水位データを2回連続で取得できませんでした。\n"
        f"エラー: {error_summary}\n"
        f"手動確認: {cfg['notify']['kanagawa_page']}"
    )


def send_slack(webhook_url: str, text: str, timeout: int = 15) -> bool:
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (URLError, TimeoutError):
        return False
