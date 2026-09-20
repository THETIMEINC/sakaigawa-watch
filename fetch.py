"""外部データ取得。I/O のみを担当し、計算は forecast.py に任せる。"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.error import URLError

USER_AGENT = "sakaigawa-watch/1.0 (+https://github.com/THETIMEINC/sakaigawa-watch)"


class FetchError(Exception):
    """取得・パースに失敗したことを示す。"""


@dataclass(frozen=True)
class Reading:
    observed_at: datetime
    value: float


@dataclass(frozen=True)
class StationSnapshot:
    generated_at: datetime | None
    readings: list[Reading]
    thresholds: dict[str, float]


def _http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except (URLError, TimeoutError) as exc:
        raise FetchError(f"HTTP取得に失敗: {url}: {exc}") from exc


_GEN_AT_RE = re.compile(r"<!--\s*(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\s*-->")

# 水位表の各行: 日付付き "09/20 18:30" または時刻のみ "18:40"、
# 直後の <span data-graph-dtkey="stage_item_data_N">値</span>
_ROW_RE = re.compile(
    r'<td class="notranslate">\s*(?:(\d{2})/(\d{2})\s+)?(\d{2}):(\d{2})\s*</td>'
    r'\s*<td>\s*<span[^>]*data-graph-dtkey="stage_item_data_\d+"[^>]*>'
    r'([0-9]+\.[0-9]+)</span>',
    re.DOTALL,
)

_THRESHOLD_LABELS = {
    "氾濫危険水位": "hanran_kiken",
    "避難判断水位": "hinan_handan",
    "氾濫注意水位": "hanran_chuui",
    "水防団待機水位": "suiboudan_taiki",
}
_THRESHOLD_ROW_RE = re.compile(
    r"<dt>(?:<span[^>]*></span>)?([^<]+)</dt>\s*<dd>\s*<span[^>]*>([0-9]+\.[0-9]+)</span>",
)


def parse_station_html(html: str, now_hint: datetime | None = None) -> StationSnapshot:
    """境川橋ページの水位表・基準水位・生成時刻を抽出する。"""
    compact = re.sub(r">\s+<", "><", html)  # 属性間の改行・空白を除去して正規表現を単純化
    compact = re.sub(r"\s*\n\s*", "", compact)

    gen_match = _GEN_AT_RE.search(html)
    generated_at = None
    if gen_match:
        y, mo, d, h, mi = (int(x) for x in gen_match.groups())
        generated_at = datetime(y, mo, d, h, mi)

    anchor_date = generated_at.date() if generated_at else (now_hint.date() if now_hint else None)

    rows = _ROW_RE.findall(compact)
    if not rows:
        raise FetchError("水位表の行を検出できませんでした（ページ構造が変わった可能性）")

    readings: list[Reading] = []
    last_date = anchor_date
    last_hm: tuple[int, int] | None = None
    for mm, dd, hh, minute, value in rows:
        h, mi = int(hh), int(minute)
        if mm and dd:
            date = datetime(anchor_date.year if anchor_date else datetime.now().year, int(mm), int(dd)).date()
        elif last_date is not None:
            date = last_date
            # 日付表記のない行で時刻が巻き戻った場合は日跨ぎとみなす
            if last_hm is not None and (h, mi) < last_hm:
                date = date + timedelta(days=1)
        else:
            raise FetchError("観測日を特定できませんでした")
        last_date, last_hm = date, (h, mi)
        observed_at = datetime(date.year, date.month, date.day, h, mi)
        readings.append(Reading(observed_at=observed_at, value=float(value)))

    thresholds: dict[str, float] = {}
    for label, value in _THRESHOLD_ROW_RE.findall(compact):
        key = _THRESHOLD_LABELS.get(label.strip())
        if key:
            thresholds[key] = float(value)

    return StationSnapshot(generated_at=generated_at, readings=readings, thresholds=thresholds)


def fetch_station(url: str) -> StationSnapshot:
    html = _http_get(url)
    return parse_station_html(html, now_hint=datetime.now())


@dataclass(frozen=True)
class RainForecastPoint:
    time: datetime
    precipitation_mm: float


def fetch_rain_forecast(latitude: float, longitude: float, timeout: int = 15) -> list[RainForecastPoint] | None:
    """今後3時間の15分刻み降水量予測。失敗時は None（通知は継続する）。"""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&minutely_15=precipitation&forecast_days=1&timezone=Asia%2FTokyo&models=jma_seamless"
    )
    try:
        raw = _http_get(url, timeout=timeout)
        data = json.loads(raw)
        times = data["minutely_15"]["time"]
        values = data["minutely_15"]["precipitation"]
    except Exception:
        return None

    now = datetime.now()
    horizon = now + timedelta(hours=3)
    points: list[RainForecastPoint] = []
    for t, v in zip(times, values):
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            continue
        if now <= dt <= horizon:
            points.append(RainForecastPoint(time=dt, precipitation_mm=float(v)))
    return points


def load_config(path: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 以下のフォールバックは不要（3.12前提）
        raise
    with open(path, "rb") as f:
        return tomllib.load(f)
