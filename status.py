"""STATUS.md（現況ページ）の本文組み立て。ファイルI/Oはmain.pyが行う。"""
from __future__ import annotations

from datetime import datetime

from forecast import LEVEL_LABELS, LEVEL_ORDER, CleanedReading, Judgement, etas_from_schedule, latest_valid_reading, valid_points
from notify import OPEN_METEO_URL, format_eta_datetime, format_no_crossing_note


def _level_status_line(current_value: float | None, thresholds: dict[str, float]) -> str:
    if current_value is None:
        return "データなし"
    exceeded = [lv for lv in LEVEL_ORDER if thresholds.get(lv) is not None and current_value >= thresholds[lv]]
    if not exceeded:
        return "🟢 平常（各警戒水位未満）"
    top = exceeded[-1]
    icon = {"suiboudan_taiki": "🟡", "hanran_chuui": "🟠", "hinan_handan": "🔴", "hanran_kiken": "🟣"}[top]
    return f"{icon} {LEVEL_LABELS[top]}を超過"


def build_status_markdown(
    judgement: Judgement,
    cleaned: list[CleanedReading],
    thresholds: dict[str, float],
    now: datetime,
    generated_at: datetime | None,
    chart_relpath: str,
    camera_relpath: str | None,
    camera_captured_at: datetime | None,
    rain_total_mm: float | None,
    cfg: dict,
) -> str:
    lines: list[str] = []
    lines.append("# 境川橋 水位状況")
    lines.append("")
    lines.append(f"最終更新: {now:%Y-%m-%d %H:%M:%S}（このページはGitHub Actionsが自動生成しています）")
    lines.append("")
    lines.append(f"## {_level_status_line(judgement.current_value, thresholds)}")
    lines.append("")
    latest = latest_valid_reading(cleaned)
    if judgement.current_value is not None and latest is not None:
        lines.append(f"- 現在水位: **{judgement.current_value:.2f} m**（観測: {latest.observed_at:%Y-%m-%d %H:%M:%S}）")
        page_delay_min = None
        if generated_at is not None:
            page_delay_min = (generated_at - latest.observed_at).total_seconds() / 60.0
        if page_delay_min is not None and page_delay_min >= 20:
            lines.append(
                f"  （県ページの更新表示は{generated_at:%H:%M}時点ですが、水位表の最新値は{latest.observed_at:%H:%M}のものです）"
            )
    else:
        lines.append("- 現在水位: 取得できませんでした")

    if judgement.surge_10min or judgement.surge_30min:
        lines.append("- ⚠️ 急上昇を検知しています")
    if rain_total_mm is not None:
        lines.append(
            f"- 今後3時間の予測降水量（藤沢市周辺）: 約{rain_total_mm:.1f} mm"
            f"（出典: [Open-Meteo]({OPEN_METEO_URL})）"
        )
    lines.append("")

    lines.append("## 水位グラフ（実測・予想）")
    lines.append("")
    lines.append(f"![境川橋の水位グラフ（実測・予想）]({chart_relpath})")
    lines.append("")
    lines.append(
        "実線=実測水位、破線+帯=予想（不確実性幅）、水平線=警戒水位。"
        "予想は直近の上昇トレンドに今後の降雨予報（出典: "
        f"[Open-Meteo]({OPEN_METEO_URL})）の強弱を反映したヒューリスティックであり、"
        "検証済みの水文モデル（貯留関数法等）ではありません。雨が弱まる予報のときは"
        "上昇率が下がり平坦化しますが、水位の下降（減衰）は表現していません"
        "（下降モデルを将来検証できるよう、降雨の実測データの蓄積を開始しています）。"
        "しきい値の目前（数cm差）で「到達見込みなし」となる場合があるため、"
        "下表には予測ピーク値も併記しています。"
    )
    lines.append("")

    if camera_relpath:
        captured_str = f"{camera_captured_at:%Y-%m-%d %H:%M:%S}" if camera_captured_at else "不明"
        lines.append("## 河川カメラ映像")
        lines.append("")
        lines.append(f"水位が上昇傾向のため、最新映像を掲載しています（撮影時刻: {captured_str}）。")
        lines.append("")
        lines.append(f"![境川橋付近の河川カメラ映像]({camera_relpath})")
        lines.append("")
        lines.append(f"出典: [横浜市河川監視カメラ]({cfg['notify']['camera_page']})")
        lines.append("")

    lines.append("## 警戒水位（公式基準）と到達予想")
    lines.append("")
    lines.append("| 段階 | 水位 | 到達予想 |")
    lines.append("|---|---|---|")
    etas_by_level = {}
    if judgement.current_value is not None:
        etas_by_level = {
            e.level: e for e in etas_from_schedule(judgement.forecast_schedule, judgement.current_value, thresholds)
        }
    for level in LEVEL_ORDER:
        v = thresholds.get(level)
        if v is None:
            continue
        if judgement.current_value is not None and judgement.current_value >= v:
            eta_text = "超過済み"
        elif level in etas_by_level and judgement.forecast_schedule:
            eta = etas_by_level[level]
            anchor_time = judgement.forecast_schedule[0].time
            eta_text = format_eta_datetime(anchor_time, eta)
        elif judgement.forecast_schedule:
            eta_text = format_no_crossing_note(v, judgement.forecast_schedule)
        else:
            eta_text = "-"
        lines.append(f"| {LEVEL_LABELS[level]} | {v:.2f} m | {eta_text} |")
    lines.append("")
    lines.append(
        "到達予想は上記グラフと同じ予測パス（直近トレンド×降雨予報の比率ヒューリスティック）から算出しています。"
    )
    lines.append("")

    lines.append("## 直近の観測値")
    lines.append("")
    lines.append("| 観測時刻 | 水位(m) |")
    lines.append("|---|---|")
    recent = sorted(valid_points(cleaned), key=lambda p: p.observed_at, reverse=True)[:12]
    for p in recent:
        lines.append(f"| {p.observed_at:%m/%d %H:%M} | {p.value:.2f} |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(cfg["notify"]["disclaimer"])
    lines.append("")
    lines.append(f"詳細データ: [{cfg['notify']['kanagawa_page']}]({cfg['notify']['kanagawa_page']})")
    lines.append("")

    return "\n".join(lines) + "\n"
