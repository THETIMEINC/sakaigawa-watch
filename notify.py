"""Slack通知の整形と送信。"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from urllib.error import URLError

from datetime import timedelta

from fetch import RainPoint
from forecast import LEVEL_LABELS, EtaEstimate, Judgement, SchedulePoint

OPEN_METEO_URL = "https://open-meteo.com/"


def format_minutes(m: float) -> str:
    m = max(m, 0)
    if m < 60:
        return f"{round(m)}分"
    h = int(m // 60)
    rem = round(m % 60)
    return f"{h}時間{rem}分" if rem else f"{h}時間"


def format_eta_datetime(anchor_time: datetime, eta: EtaEstimate) -> str:
    """到達予想を「分後」ではなく実際の日付・時刻（MM/DD HH:MM）で表す。"""
    t_low = anchor_time + timedelta(minutes=eta.eta_minutes_low)
    t_high = anchor_time + timedelta(minutes=eta.eta_minutes_high)

    def fmt(t: datetime) -> str:
        return t.strftime("%m/%d %H:%M")

    if abs((t_high - t_low).total_seconds()) < 60:
        return f"{fmt(t_low)}頃"
    return f"{fmt(t_low)}〜{fmt(t_high)}頃"


def format_no_crossing_note(threshold: float, schedule: list[SchedulePoint], short: bool = False) -> str:
    """予報期間内に到達しない場合、どこまで迫っていたか（ピーク値・差）を添える。

    しきい値ギリギリ手前（例: 3cm差）でも数値上は「到達なし」と表示されるため、
    ピーク値を示さないと閲覧者に伝わりにくい（実際にあった問い合わせを踏まえて追加）。
    """
    if not schedule:
        return "見込みなし" if short else "予報期間内に到達見込みなし"

    horizon_minutes = (schedule[-1].time - schedule[0].time).total_seconds() / 60.0
    horizon_str = format_minutes(horizon_minutes)

    peak = schedule[-1].value_high  # 予測パスは単調非減少のため終点が最大値
    margin = threshold - peak
    if margin <= 0:
        return f"{horizon_str}の終盤に到達の可能性" if short else f"{horizon_str}以内の終盤に到達する可能性があります"
    if short:
        return f"{horizon_str}以内は未達予定(ピーク{peak:.2f}m)"
    return f"{horizon_str}以内では未達予定（予測ピークは約{peak:.2f}m、あと{margin:.2f}m）"


def build_message(
    judgement: Judgement,
    newly_notified_levels: list[str],
    rain: list[RainPoint] | None,
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

    anchor_time = judgement.forecast_schedule[0].time if judgement.forecast_schedule else None
    reported_eta = False
    for eta in judgement.approaching:
        if eta.level in newly_notified_levels:
            if anchor_time is not None:
                lines.append(f"・{LEVEL_LABELS[eta.level]}到達予想: {format_eta_datetime(anchor_time, eta)}")
            else:
                lines.append(
                    f"・{LEVEL_LABELS[eta.level]}まで約 {format_minutes(eta.eta_minutes_low)}〜{format_minutes(eta.eta_minutes_high)}"
                )
            reported_eta = True
    if reported_eta:
        lines.append("（この予測は今後の降雨予報を加味したヒューリスティックで、検証済みの水文モデルではありません）")

    if judgement.surge_10min:
        lines.append("・直近10分で急上昇（+0.15m以上）")
    if judgement.surge_30min:
        lines.append("・直近30分で急上昇（+0.3m以上）")

    if rain:
        total = sum(p.precipitation_mm for p in rain)
        if total > 0:
            lines.append(f"・今後3時間の予測降水量: 約{total:.1f}mm（藤沢市周辺、出典: Open-Meteo {OPEN_METEO_URL}）")

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
