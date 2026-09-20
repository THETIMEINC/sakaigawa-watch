"""水位グラフのSVG生成。外部ライブラリに依存せず標準ライブラリのみで描画する。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

from forecast import LEVEL_LABELS, LEVEL_ORDER, CleanedReading, SchedulePoint, etas_from_schedule, valid_points
from notify import OPEN_METEO_URL, format_eta_datetime, format_no_crossing_note

WIDTH = 760
HEIGHT = 380
MARGIN_LEFT = 55
MARGIN_RIGHT = 90
MARGIN_TOP = 20
MARGIN_BOTTOM = 40

PLOT_LEFT = MARGIN_LEFT
PLOT_RIGHT = WIDTH - MARGIN_RIGHT
PLOT_TOP = MARGIN_TOP
PLOT_BOTTOM = HEIGHT - MARGIN_BOTTOM

THRESHOLD_COLORS = {
    "suiboudan_taiki": "#c9a227",
    "hanran_chuui": "#e08a1e",
    "hinan_handan": "#d94f2b",
    "hanran_kiken": "#c0392b",
}


@dataclass(frozen=True)
class ChartResult:
    svg: str
    x_min: datetime
    x_max: datetime


def _scale_x(t: datetime, x_min: datetime, x_max: datetime) -> float:
    span = (x_max - x_min).total_seconds()
    if span <= 0:
        return PLOT_LEFT
    ratio = (t - x_min).total_seconds() / span
    ratio = min(max(ratio, 0.0), 1.0)
    return PLOT_LEFT + ratio * (PLOT_RIGHT - PLOT_LEFT)


def _scale_y(v: float, y_min: float, y_max: float) -> float:
    span = y_max - y_min
    if span <= 0:
        return PLOT_BOTTOM
    ratio = (v - y_min) / span
    ratio = min(max(ratio, 0.0), 1.0)
    return PLOT_BOTTOM - ratio * (PLOT_BOTTOM - PLOT_TOP)


def build_svg_chart(
    cleaned: list[CleanedReading],
    thresholds: dict[str, float],
    now: datetime,
    schedule: list[SchedulePoint],
    window_hours: int = 24,
    forecast_hours: int = 3,
) -> ChartResult:
    """実測水位・警戒水位・（あれば）予想パスを1枚のSVGにまとめる。

    予想パス（schedule）は forecast.judge() が計算したものをそのまま渡す。
    通知のETA計算と同じ元データを描画するため、グラフと通知の予測が食い違わない。
    """
    x_min = now - timedelta(hours=window_hours)
    x_max = now + timedelta(hours=forecast_hours)

    points = [p for p in valid_points(cleaned) if p.observed_at >= x_min]
    values = [p.value for p in points] or [0.0]
    threshold_values = list(thresholds.values()) or [5.65]

    y_min = min(0.0, min(values) - 0.3)
    y_max = max(max(threshold_values) + 0.4, max(values) + 0.3)

    forecast_points: list[tuple[datetime, float, float]] = [
        (p.time, p.value_low, p.value_high) for p in schedule
    ]
    if forecast_points:
        y_max = max(y_max, max(h for _, _, h in forecast_points) + 0.2)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'font-family="sans-serif" font-size="11">'
    )
    parts.append(f'<rect x="0" y="0" width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>')

    # 枠
    parts.append(
        f'<rect x="{PLOT_LEFT}" y="{PLOT_TOP}" width="{PLOT_RIGHT - PLOT_LEFT}" '
        f'height="{PLOT_BOTTOM - PLOT_TOP}" fill="none" stroke="#ccc"/>'
    )

    # しきい値の水平線
    for level in LEVEL_ORDER:
        v = thresholds.get(level)
        if v is None:
            continue
        y = _scale_y(v, y_min, y_max)
        color = THRESHOLD_COLORS.get(level, "#999")
        parts.append(
            f'<line x1="{PLOT_LEFT}" y1="{y:.1f}" x2="{PLOT_RIGHT}" y2="{y:.1f}" '
            f'stroke="{color}" stroke-dasharray="4,3" stroke-width="1"/>'
        )
        label = escape(f"{LEVEL_LABELS[level]} {v:.2f}m")
        parts.append(f'<text x="{PLOT_RIGHT + 5}" y="{y + 3:.1f}" fill="{color}">{label}</text>')

    # 「現在時刻」の縦線
    now_x = _scale_x(now, x_min, x_max)
    parts.append(
        f'<line x1="{now_x:.1f}" y1="{PLOT_TOP}" x2="{now_x:.1f}" y2="{PLOT_BOTTOM}" '
        f'stroke="#888" stroke-dasharray="2,3" stroke-width="1"/>'
    )
    parts.append(f'<text x="{now_x + 3:.1f}" y="{PLOT_TOP + 12}" fill="#888">現在</text>')

    # x軸目盛り（時刻）
    tick_count = 6
    for i in range(tick_count + 1):
        t = x_min + (x_max - x_min) * (i / tick_count)
        x = _scale_x(t, x_min, x_max)
        parts.append(f'<line x1="{x:.1f}" y1="{PLOT_BOTTOM}" x2="{x:.1f}" y2="{PLOT_BOTTOM + 4}" stroke="#666"/>')
        label = t.strftime("%m/%d %H:%M")
        parts.append(
            f'<text x="{x:.1f}" y="{PLOT_BOTTOM + 16}" text-anchor="middle" fill="#333">{label}</text>'
        )

    # y軸目盛り
    y_tick_count = 5
    for i in range(y_tick_count + 1):
        v = y_min + (y_max - y_min) * (i / y_tick_count)
        y = _scale_y(v, y_min, y_max)
        parts.append(f'<line x1="{PLOT_LEFT - 4}" y1="{y:.1f}" x2="{PLOT_LEFT}" y2="{y:.1f}" stroke="#666"/>')
        parts.append(f'<text x="{PLOT_LEFT - 8}" y="{y + 3:.1f}" text-anchor="end" fill="#333">{v:.1f}</text>')

    # 予想（不確実性の幅を帯で、中心線を破線で）
    if forecast_points:
        band = []
        for t, low, _ in forecast_points:
            band.append(f"{_scale_x(t, x_min, x_max):.1f},{_scale_y(low, y_min, y_max):.1f}")
        for t, _, high in reversed(forecast_points):
            band.append(f"{_scale_x(t, x_min, x_max):.1f},{_scale_y(high, y_min, y_max):.1f}")
        parts.append(f'<polygon points="{" ".join(band)}" fill="#2e7dd7" fill-opacity="0.12" stroke="none"/>')

        mid_line = []
        for p in schedule:
            mid_line.append(f"{_scale_x(p.time, x_min, x_max):.1f},{_scale_y(p.value_mid, y_min, y_max):.1f}")
        parts.append(
            f'<polyline points="{" ".join(mid_line)}" fill="none" stroke="#2e7dd7" '
            f'stroke-width="1.5" stroke-dasharray="5,3"/>'
        )

    # 実測データ
    if points:
        line = " ".join(
            f"{_scale_x(p.observed_at, x_min, x_max):.1f},{_scale_y(p.value, y_min, y_max):.1f}" for p in points
        )
        parts.append(f'<polyline points="{line}" fill="none" stroke="#1a1a1a" stroke-width="2"/>')

        latest = points[-1]
        lx = _scale_x(latest.observed_at, x_min, x_max)
        ly = _scale_y(latest.value, y_min, y_max)
        parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" fill="#1a1a1a"/>')
        parts.append(f'<text x="{lx + 6:.1f}" y="{ly - 6:.1f}" fill="#1a1a1a">{latest.value:.2f}m</text>')

    # 到達予想時間のボックス（各警戒水位への到達予想を左上にまとめて表示）
    if points:
        current_value = points[-1].value
        etas_by_level = {e.level: e for e in etas_from_schedule(schedule, current_value, thresholds)}

        anchor_time = schedule[0].time if schedule else None
        rows: list[tuple[str, str, str]] = []  # (label, text, color)
        for level in LEVEL_ORDER:
            threshold = thresholds.get(level)
            if threshold is None:
                continue
            color = THRESHOLD_COLORS.get(level, "#333")
            label = LEVEL_LABELS[level]
            if current_value >= threshold:
                rows.append((label, "超過済み", color))
                continue
            eta = etas_by_level.get(level)
            if eta is None:
                rows.append((label, format_no_crossing_note(threshold, schedule, short=True), "#888"))
            elif anchor_time is not None:
                rows.append((label, format_eta_datetime(anchor_time, eta), color))

        box_x = PLOT_LEFT + 8
        box_y = PLOT_TOP + 8
        box_w = 280
        box_h = 16 + 15 * len(rows) + 6
        parts.append(
            f'<rect x="{box_x}" y="{box_y}" width="{box_w}" height="{box_h}" '
            f'fill="#ffffff" fill-opacity="0.9" stroke="#ccc"/>'
        )
        parts.append(f'<text x="{box_x + 6}" y="{box_y + 14}" fill="#333" font-weight="bold">到達予想</text>')
        for i, (label, text, color) in enumerate(rows):
            y = box_y + 14 + 15 * (i + 1)
            parts.append(f'<text x="{box_x + 6}" y="{y}" fill="{color}">{escape(label)}: {escape(text)}</text>')

    parts.append(
        f'<text x="{PLOT_LEFT}" y="{HEIGHT - 6}" fill="#666">'
        f"実線: 実測水位　破線+帯: 予想（不確実性幅、降雨予報:Open-Meteo {OPEN_METEO_URL}）"
        f"　凡例の水平線: 警戒水位</text>"
    )

    parts.append("</svg>")
    return ChartResult(svg="\n".join(parts), x_min=x_min, x_max=x_max)
