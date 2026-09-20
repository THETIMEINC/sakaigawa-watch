"""純粋計算のみ（外部I/O・時刻取得なし）。テストしやすさのため状態を持たない。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fetch import Reading

# 段階名は水位の低い順（config.toml の thresholds キーと対応）
LEVEL_ORDER = ["suiboudan_taiki", "hanran_chuui", "hinan_handan", "hanran_kiken"]
LEVEL_LABELS = {
    "suiboudan_taiki": "水防団待機水位",
    "hanran_chuui": "氾濫注意水位",
    "hinan_handan": "避難判断水位",
    "hanran_kiken": "氾濫危険水位",
}


@dataclass(frozen=True)
class CleanedReading:
    observed_at: datetime
    value: float
    suspect: bool


def clean_readings(readings: list[Reading], max_jump_per_10min: float) -> list[CleanedReading]:
    """範囲外の値と、前値からの急な跳びを疑義（suspect）として除外対象にする。

    速報値は観測機器の故障・通信異常でそのまま異常値が載ることが県ページに明記されている。
    ここでは「予測に使わないが記録には残す」ため、除外はせず suspect フラグのみ付与する。
    """
    ordered = sorted(readings, key=lambda r: r.observed_at)
    result: list[CleanedReading] = []
    prev: CleanedReading | None = None
    for r in ordered:
        out_of_range = not (0.0 <= r.value <= 10.0)
        jump = False
        if prev is not None and not prev.suspect:
            minutes = max((r.observed_at - prev.observed_at).total_seconds() / 60.0, 1.0)
            allowed = max_jump_per_10min * (minutes / 10.0)
            jump = abs(r.value - prev.value) > allowed
        suspect = out_of_range or jump
        cur = CleanedReading(observed_at=r.observed_at, value=r.value, suspect=suspect)
        result.append(cur)
        prev = cur
    return result


def valid_points(cleaned: list[CleanedReading]) -> list[CleanedReading]:
    return [c for c in cleaned if not c.suspect]


def latest_valid_reading(cleaned: list[CleanedReading]) -> CleanedReading | None:
    points = valid_points(cleaned)
    if not points:
        return None
    return max(points, key=lambda p: p.observed_at)


@dataclass(frozen=True)
class TrendFit:
    slope_per_min: float
    intercept: float
    anchor_time: datetime
    residual_std: float
    n: int


def fit_trend(
    cleaned: list[CleanedReading],
    anchor: datetime,
    window_minutes: int,
    min_points: int,
) -> TrendFit | None:
    """直近 window_minutes 以内の有効点に単純最小二乗で直線を当てる。

    anchor には「壁時計の現在時刻」ではなく最新の観測時刻を渡すこと。
    県ページのデータ配信が数十分遅れることがあり、壁時計を基準にすると
    直近ウィンドウに点が入らず予測が静かに消えてしまうため。
    """
    points = [
        c for c in valid_points(cleaned)
        if (anchor - c.observed_at).total_seconds() / 60.0 <= window_minutes
    ]
    if len(points) < min_points:
        return None

    xs = [(p.observed_at - anchor).total_seconds() / 60.0 for p in points]  # 分単位、過去は負
    ys = [p.value for p in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    residual_std = (sum(r * r for r in residuals) / n) ** 0.5

    return TrendFit(slope_per_min=slope, intercept=intercept, anchor_time=anchor, residual_std=residual_std, n=n)


@dataclass(frozen=True)
class EtaEstimate:
    level: str
    eta_minutes_low: float
    eta_minutes_high: float


@dataclass(frozen=True)
class RainContext:
    """降雨情報（I/Oなし。main.pyがOpen-Meteoの結果から組み立てる）。

    recent_mm: 直近の実測降雨合計（trend_window_minutes と同じ期間）。
    future_buckets: (anchorからの経過分, その15分間の予報降水量mm) のリスト。
    """
    recent_mm: float
    future_buckets: list[tuple[float, float]]


@dataclass(frozen=True)
class SchedulePoint:
    time: datetime
    value_low: float
    value_mid: float
    value_high: float


def build_rain_ratio_schedule(
    trend: TrendFit,
    current_value: float,
    rain: RainContext | None,
    cfg: dict,
) -> list[SchedulePoint]:
    """降雨予報の強弱に応じて上昇率を時間刻みで補正した予測パスを作る。

    根拠: 小規模な都市河川は降雨強度の変化に比較的速く追随する（本プロジェクトの
    実データでも降雨ピークの直後に水位が追随して上昇したことを確認済み）という
    前提のヒューリスティックであり、検証済みの水文モデル（貯留関数法等）ではない。

    直近の降雨強度に対する将来の降雨強度の比率で、直近トレンドの上昇率をスケールする。
    雨が弱まれば上昇率も下がり、止めば上昇率は0（平坦化）になる——ただし「水位が
    下降する速さ」は未検証のため下限は0までで、下降は表現しない。

    現在の上昇が降雨由来と言えない場合（recent_mm が rain_floor_mm 未満。上流放流・
    感潮等の可能性）や降雨データが取得できない場合（rain is None）は、比率を常に1に
    固定し、従来通りの単純な線形延長にフォールバックする。
    """
    if trend.slope_per_min <= 0:
        return []

    spread = trend.residual_std
    window_minutes = cfg["forecast"]["trend_window_minutes"]
    rain_floor = cfg["forecast"].get("rain_floor_mm", 1.0)
    rain_ratio_max = cfg["forecast"].get("rain_ratio_max", 3.0)

    rain_driven = rain is not None and rain.recent_mm >= rain_floor
    recent_rate_per_15 = (rain.recent_mm / (window_minutes / 15.0)) if rain_driven else None
    future_buckets = sorted(rain.future_buckets) if (rain_driven and rain is not None) else []

    step_minutes = 15.0
    horizon_minutes = max(
        cfg["forecast"].get("eta_warn_minutes_chuui", 180),
        cfg["forecast"].get("eta_warn_minutes_handan", 0),
        cfg["forecast"].get("eta_warn_minutes_kiken", 0),
    )
    steps = max(int(horizon_minutes // step_minutes), 1)

    schedule: list[SchedulePoint] = [
        SchedulePoint(time=trend.anchor_time, value_low=current_value, value_mid=current_value, value_high=current_value)
    ]
    value_low = value_mid = value_high = current_value

    for i in range(steps):
        offset_start = i * step_minutes
        if rain_driven:
            bucket_mm = next(
                (mm for off, mm in future_buckets if abs(off - offset_start) < step_minutes / 2),
                0.0,
            )
            ratio = min(max(bucket_mm / max(recent_rate_per_15, 1e-6), 0.0), rain_ratio_max)
        else:
            ratio = 1.0  # 従来通りの単純線形

        slope_mid = trend.slope_per_min * ratio
        slope_low = max(slope_mid - spread / 30.0, 0.0)  # 平坦化まで（下降は表現しない）
        slope_high = slope_mid + spread / 30.0

        value_mid += slope_mid * step_minutes
        value_low += slope_low * step_minutes
        value_high += slope_high * step_minutes
        t = trend.anchor_time + timedelta(minutes=offset_start + step_minutes)
        schedule.append(SchedulePoint(time=t, value_low=value_low, value_mid=value_mid, value_high=value_high))

    return schedule


def etas_from_schedule(
    schedule: list[SchedulePoint],
    current_value: float,
    thresholds: dict[str, float],
) -> list[EtaEstimate]:
    """予測パスを先頭から走査し、各段階への到達時刻幅を求める。

    グラフの予測線（chart.py）と同じスケジュールを使うため、通知のETAと
    グラフの見た目が食い違わない。予報期間内に到達しない段階はETAを出さない
    （＝雨が弱まる予報のときは自然にETAが遠のく・消える）。
    """
    if not schedule:
        return []
    anchor_time = schedule[0].time
    horizon_minutes = (schedule[-1].time - anchor_time).total_seconds() / 60.0
    results: list[EtaEstimate] = []

    for level in LEVEL_ORDER:
        threshold = thresholds.get(level)
        if threshold is None or threshold <= current_value:
            continue

        def first_cross(attr: str) -> float | None:
            for point in schedule:
                if getattr(point, attr) >= threshold:
                    return (point.time - anchor_time).total_seconds() / 60.0
            return None

        fastest = first_cross("value_high")
        slowest = first_cross("value_low")
        if fastest is None:
            continue  # 最も早いシナリオでも到達しない＝差し迫っていない
        results.append(
            EtaEstimate(level=level, eta_minutes_low=fastest, eta_minutes_high=slowest or horizon_minutes)
        )
    return results


def change_over(cleaned: list[CleanedReading], minutes: int) -> float | None:
    """最新の観測値から minutes 分前までの変化量。最新観測時刻を基準にするため壁時計は不要。"""
    points = valid_points(cleaned)
    if not points:
        return None
    latest = max(points, key=lambda p: p.observed_at)
    target_time = latest.observed_at - timedelta(minutes=minutes)
    candidates = [p for p in points if p.observed_at <= target_time]
    if not candidates:
        return None
    base = max(candidates, key=lambda p: p.observed_at)
    return latest.value - base.value


@dataclass(frozen=True)
class Judgement:
    current_value: float | None
    current_level: str | None  # 超えている最も高い段階（超えていなければ None）
    exceeded_levels: list[str] = field(default_factory=list)
    approaching: list[EtaEstimate] = field(default_factory=list)
    surge_10min: bool = False
    surge_30min: bool = False
    no_data: bool = False
    forecast_schedule: list[SchedulePoint] = field(default_factory=list)


def judge(
    cleaned: list[CleanedReading],
    now: datetime,
    thresholds: dict[str, float],
    cfg: dict,
    rain: RainContext | None = None,
) -> Judgement:
    points = valid_points(cleaned)
    if not points:
        return Judgement(current_value=None, current_level=None, no_data=True)

    latest = max(points, key=lambda p: p.observed_at)
    current_value = latest.value

    exceeded = [lv for lv in LEVEL_ORDER if thresholds.get(lv) is not None and current_value >= thresholds[lv]]
    current_level = exceeded[-1] if exceeded else None

    base_level = thresholds.get("suiboudan_taiki")
    below_base = base_level is not None and current_value < base_level

    # 最新観測時刻を基準にする（壁時計を基準にすると、県ページの配信遅延で
    # 直近ウィンドウに点が入らず予測が静かに消えてしまうため）。
    # ただし配信がそもそも長時間止まっている場合は、古いデータから
    # 予測を作らないよう stale_after_minutes を超えたら評価しない。
    stale_after = cfg["forecast"].get("stale_after_minutes", 90)
    staleness_min = (now - latest.observed_at).total_seconds() / 60.0
    stale = staleness_min > stale_after

    schedule: list[SchedulePoint] = []
    approaching: list[EtaEstimate] = []
    surge10 = surge30 = False

    if not stale:
        trend = fit_trend(
            cleaned,
            latest.observed_at,
            window_minutes=cfg["forecast"]["trend_window_minutes"],
            min_points=cfg["forecast"]["min_valid_points"],
        )
        if trend is None:
            trend = fit_trend(
                cleaned,
                latest.observed_at,
                window_minutes=cfg["forecast"]["trend_window_minutes_min"],
                min_points=cfg["forecast"]["min_valid_points"],
            )
        if trend is not None:
            # スケジュール（グラフの予測線）は below_base でも作る。通知の可否だけを
            # below_base で絞る（平常時の変動で鳴り続けないようにする既存方針）。
            schedule = build_rain_ratio_schedule(trend, current_value, rain, cfg)

        if not below_base:
            if schedule:
                etas = etas_from_schedule(schedule, current_value, thresholds)
                warn_minutes = {
                    "hanran_chuui": cfg["forecast"]["eta_warn_minutes_chuui"],
                    "hinan_handan": cfg["forecast"]["eta_warn_minutes_handan"],
                    "hanran_kiken": cfg["forecast"]["eta_warn_minutes_kiken"],
                }
                approaching = [e for e in etas if e.eta_minutes_low <= warn_minutes.get(e.level, 0)]

            d10 = change_over(cleaned, 10)
            d30 = change_over(cleaned, 30)
            surge10 = d10 is not None and d10 >= cfg["forecast"]["surge_10min"]
            surge30 = d30 is not None and d30 >= cfg["forecast"]["surge_30min"]

    return Judgement(
        current_value=current_value,
        current_level=current_level,
        exceeded_levels=exceeded,
        approaching=approaching,
        surge_10min=surge10,
        surge_30min=surge30,
        forecast_schedule=schedule,
    )


def should_run_full_check(
    last_value: float | None,
    last_checked_at: datetime | None,
    now: datetime,
    cfg: dict,
) -> bool:
    """平常時は間引き、警戒水位に近づいたら毎回実行する。

    県ページは常に直近約4時間分のデータを返すため、間引いても
    次回アクセス時にまとめて取得でき、データの解像度は失われない。
    """
    if last_value is None or last_checked_at is None:
        return True

    escalate_threshold = cfg["thresholds"]["suiboudan_taiki"] - cfg["schedule"]["escalate_margin"]
    if last_value >= escalate_threshold:
        return True

    elapsed_min = (now - last_checked_at).total_seconds() / 60.0
    return elapsed_min >= cfg["schedule"]["normal_interval_minutes"]
