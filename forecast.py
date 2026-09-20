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


@dataclass(frozen=True)
class TrendFit:
    slope_per_min: float
    intercept: float
    anchor_time: datetime
    residual_std: float
    n: int


def fit_trend(
    cleaned: list[CleanedReading],
    now: datetime,
    window_minutes: int,
    min_points: int,
) -> TrendFit | None:
    """直近 window_minutes 以内の有効点に単純最小二乗で直線を当てる。"""
    points = [
        c for c in valid_points(cleaned)
        if (now - c.observed_at).total_seconds() / 60.0 <= window_minutes
    ]
    if len(points) < min_points:
        return None

    xs = [(p.observed_at - now).total_seconds() / 60.0 for p in points]  # 分単位、過去は負
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

    return TrendFit(slope_per_min=slope, intercept=intercept, anchor_time=now, residual_std=residual_std, n=n)


@dataclass(frozen=True)
class EtaEstimate:
    level: str
    eta_minutes_low: float
    eta_minutes_high: float


def estimate_etas(trend: TrendFit, current_value: float, thresholds: dict[str, float]) -> list[EtaEstimate]:
    """上昇中（slope>0）の場合のみ、まだ超えていない各段階への到達時刻幅を返す。"""
    if trend.slope_per_min <= 0:
        return []
    results: list[EtaEstimate] = []
    for level in LEVEL_ORDER:
        threshold = thresholds.get(level)
        if threshold is None or threshold <= current_value:
            continue
        eta_mid = (threshold - trend.intercept) / trend.slope_per_min
        if eta_mid < 0:
            continue
        # 残差の標準偏差ぶんだけ傾きに幅を持たせ、到達時刻の早い側・遅い側を出す
        spread = trend.residual_std
        slope_fast = trend.slope_per_min + spread / 30.0
        slope_slow = max(trend.slope_per_min - spread / 30.0, 1e-6)
        eta_fast = (threshold - trend.intercept) / slope_fast if slope_fast > 0 else eta_mid
        eta_slow = (threshold - trend.intercept) / slope_slow
        low = max(min(eta_fast, eta_slow), 0.0)
        high = max(eta_fast, eta_slow)
        results.append(EtaEstimate(level=level, eta_minutes_low=low, eta_minutes_high=high))
    return results


def change_over(cleaned: list[CleanedReading], now: datetime, minutes: int) -> float | None:
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


def judge(
    cleaned: list[CleanedReading],
    now: datetime,
    thresholds: dict[str, float],
    cfg: dict,
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

    approaching: list[EtaEstimate] = []
    surge10 = surge30 = False
    if not below_base:
        trend = fit_trend(
            cleaned,
            now,
            window_minutes=cfg["forecast"]["trend_window_minutes"],
            min_points=cfg["forecast"]["min_valid_points"],
        )
        if trend is None:
            trend = fit_trend(
                cleaned,
                now,
                window_minutes=cfg["forecast"]["trend_window_minutes_min"],
                min_points=cfg["forecast"]["min_valid_points"],
            )
        if trend is not None:
            etas = estimate_etas(trend, current_value, thresholds)
            warn_minutes = {
                "hanran_chuui": cfg["forecast"]["eta_warn_minutes_chuui"],
                "hinan_handan": cfg["forecast"]["eta_warn_minutes_handan"],
                "hanran_kiken": cfg["forecast"]["eta_warn_minutes_kiken"],
            }
            approaching = [e for e in etas if e.eta_minutes_low <= warn_minutes.get(e.level, 0)]

        d10 = change_over(cleaned, now, 10)
        d30 = change_over(cleaned, now, 30)
        surge10 = d10 is not None and d10 >= cfg["forecast"]["surge_10min"]
        surge30 = d30 is not None and d30 >= cfg["forecast"]["surge_30min"]

    return Judgement(
        current_value=current_value,
        current_level=current_level,
        exceeded_levels=exceeded,
        approaching=approaching,
        surge_10min=surge10,
        surge_30min=surge30,
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
