#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import random
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, unquote

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

try:
    import requests
except Exception as exc:
    print(f"[FATAL] Failed to import requests: {exc}", flush=True)
    print("[HINT] Run: py -m pip install requests", flush=True)
    print()
    input("Press Enter to exit...")
    raise


# ============================================================
# CONFIG
# ============================================================
API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
CHECK_INTERVAL_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 30
HEARTBEAT_EVERY_SECONDS = 600

MAX_WORKERS = 6
MAX_REQUESTS_PER_SECOND = 6.0
REQUEST_JITTER_SECONDS = 0.15

RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 120.0
ERROR_COOLDOWN_SECONDS = 30.0
NO_DATA_COOLDOWN_SECONDS = 60.0

DISCOVERY_CACHE_PATH = "weather_monitor_discovery_cache.json"
STATE_CACHE_PATH = "weather_monitor_runtime_state.json"
SAVE_STATE_EVERY_POLLS = 12

LOG_TO_FILE = False
LOG_FILE_PATH = "weather_history_api_truth_monitor.log"

WRITE_JSONL_EVENTS = True
JSONL_FILE_PATH = "weather_history_api_truth_monitor.jsonl"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

# Сюди вставляйте station-level URL без дати.
# Якщо вставите URL з /date/YYYY-M-D, дата все одно буде відкинута,
# а програма автоматично візьме поточну локальну добу станції.
TARGET_STATION_URLS: List[str] = [
    "https://www.wunderground.com/history/daily/ar/ezeiza/SAEZ",
    "https://www.wunderground.com/history/daily/br/guarulhos/SBGR",
    "https://www.wunderground.com/history/daily/ca/mississauga/CYYZ",
    "https://www.wunderground.com/history/daily/cn/beijing/ZBAA",
    "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
    "https://www.wunderground.com/history/daily/cn/chongqing/ZUCK",
    "https://www.wunderground.com/history/daily/cn/shanghai/ZSPD",
    "https://www.wunderground.com/history/daily/cn/shenzhen/ZGSZ",
    "https://www.wunderground.com/history/daily/cn/wuhan/ZHHH",
    "https://www.wunderground.com/history/daily/de/munich/EDDM",
    "https://www.wunderground.com/history/daily/es/madrid/LEMD",
    "https://www.wunderground.com/history/daily/fi/vantaa/EFHK",
    "https://www.wunderground.com/history/daily/fr/paris/LFPG",
    "https://www.wunderground.com/history/daily/gb/london/EGLC",
    "https://www.wunderground.com/history/daily/id/jakarta/WIHH",
    "https://www.wunderground.com/history/daily/in/lucknow/VIL",
    "https://www.wunderground.com/history/daily/it/milan/LIMC",
    "https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
    "https://www.wunderground.com/history/daily/kr/busan/RKPK",
    "https://www.wunderground.com/history/daily/kr/incheon/RKSI",
    "https://www.wunderground.com/history/daily/mx/mexico-city/MMMX",
    "https://www.wunderground.com/history/daily/my/sepang-district/WMKK",
    "https://www.wunderground.com/history/daily/nl/schiphol/EHAM",
    "https://www.wunderground.com/history/daily/nz/wellington/NZWN",
    "https://www.wunderground.com/history/daily/pa/panama-city/MPMG",
    "https://www.wunderground.com/history/daily/pl/warsaw/EPWA",
    "https://www.wunderground.com/history/daily/sg/singapore/WSSS",
    "https://www.wunderground.com/history/daily/tr/%C3%A7ubuk/LTAC",
    "https://www.wunderground.com/history/daily/tw/taipei/RCSS",
    "https://www.wunderground.com/history/daily/us/ca/los-angeles/KLAX",
    "https://www.wunderground.com/history/daily/us/ca/san-francisco/KSFO",
    "https://www.wunderground.com/history/daily/us/co/aurora/KBKF",
    "https://www.wunderground.com/history/daily/us/fl/miami/KMIA",
    "https://www.wunderground.com/history/daily/us/ga/atlanta/KATL",
    "https://www.wunderground.com/history/daily/us/il/chicago/KORD",
    "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
    "https://www.wunderground.com/history/daily/us/tx/austin/KAUS",
    "https://www.wunderground.com/history/daily/us/tx/dallas/KDAL",
    "https://www.wunderground.com/history/daily/us/tx/houston/KHOU",
    "https://www.wunderground.com/history/daily/us/wa/seatac/KSEA",
]

STREAM_TO_UNITS: Dict[str, str] = {
    "F": "e",
    "C": "m",
}


# ============================================================
# DATA MODELS
# ============================================================
@dataclass
class TruthRow:
    target_name: str
    page_url: str
    local_date_ymd: str
    stream_unit: str
    time_text: str
    value_raw: Decimal
    row_index: int
    row_count: int
    condition_text: str
    source_mode: str
    api_url: str
    obs_time_local: str
    obs_time_utc: str
    valid_time_gmt: int
    raw_json: Dict[str, Any]
    first_seen_epoch: float = 0.0
    first_seen_wall: str = ""


@dataclass
class ParsedStationSpec:
    name_base: str
    station_page_url: str
    country_code: str
    place_slug: str
    station_code: str
    guessed_location_id: str


@dataclass
class DiscoveryInfo:
    location_id: str
    guessed_location_id: str
    geocode: str
    timezone_name: str
    timezone_offset_minutes: int


@dataclass
class TargetState:
    name: str
    station_page_url: str
    station_code: str
    location_id: str
    guessed_location_id: str
    geocode: str
    timezone_name: str
    timezone_offset_minutes: int
    stream_unit: str
    local_date_ymd: str = ""
    page_url: str = ""
    compact_date: str = ""
    api_url: str = ""
    last_identity: Optional[str] = None
    last_value_text: str = ""
    last_time_text: str = ""
    last_valid_time_gmt: int = 0
    seen_rows: int = 0
    change_count: int = 0
    error_count: int = 0
    no_data_count: int = 0
    fail_streak: int = 0
    next_poll_epoch: float = 0.0
    is_disabled: bool = False
    disabled_reason: str = ""
    last_ok_wall: str = ""
    last_err_wall: str = ""
    last_no_data_wall: str = ""


@dataclass
class MonitorState:
    poll_count: int = 0
    targets: Dict[str, TargetState] = field(default_factory=dict)
    last_heartbeat_epoch: float = 0.0


class NoDataRecordedError(RuntimeError):
    pass


# ============================================================
# LOGGING
# ============================================================
class Logger:
    def __init__(self, text_path: Optional[str], jsonl_path: Optional[str]) -> None:
        self.text_path = Path(text_path).resolve() if text_path else None
        self.jsonl_path = Path(jsonl_path).resolve() if jsonl_path else None

        if self.text_path:
            self.text_path.parent.mkdir(parents=True, exist_ok=True)
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def write(self, tag: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        line = f"[{self.now_str()}] [{tag}] {message}"
        print(line, flush=True)

        if self.text_path:
            with self.text_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

        if self.jsonl_path and payload is not None:
            obj = {
                "ts_local": self.now_str(),
                "tag": tag,
                "message": message,
                "payload": payload,
            }
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


LOGGER = Logger(LOG_FILE_PATH if LOG_TO_FILE else None, JSONL_FILE_PATH if WRITE_JSONL_EVENTS else None)


def log(tag: str, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
    LOGGER.write(tag, message, payload)


# ============================================================
# HELPERS
# ============================================================
def parse_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Invalid decimal for {field_name}: {value!r}") from exc


def fmt_decimal(value: Decimal, places: int = 3) -> str:
    q = Decimal("1").scaleb(-places)
    return f"{value.quantize(q):f}"


def decimal_key_text(value_text: str) -> str:
    try:
        value = Decimal(str(value_text))
        return fmt_decimal(value, 6)
    except Exception:
        return str(value_text)


def normalize_unit(unit: str) -> str:
    unit2 = str(unit or "").strip().upper()
    if unit2 not in {"C", "F"}:
        raise RuntimeError(f"Unsupported stream unit: {unit!r}")
    return unit2


def truth_identity(row: TruthRow) -> str:
    return f"{row.local_date_ymd}|{row.stream_unit}|{row.time_text}|{fmt_decimal(row.value_raw, 6)}|{row.condition_text}"


def format_display_time(dt: datetime) -> str:
    hour_12 = dt.strftime("%I").lstrip("0")
    if not hour_12:
        hour_12 = "0"
    return f"{hour_12}:{dt.strftime('%M %p')}"


def parse_datetime_flexible(value: str) -> Optional[datetime]:
    s = str(value or "").strip()
    if not s:
        return None

    candidates = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def build_fixed_timezone(offset_minutes: int) -> timezone:
    return timezone(timedelta(minutes=int(offset_minutes)))


def choose_station_timezone(timezone_name: str, timezone_offset_minutes: int) -> timezone:
    if timezone_name and ZoneInfo is not None:
        try:
            return ZoneInfo(timezone_name)  # type: ignore[arg-type]
        except Exception:
            pass
    return build_fixed_timezone(timezone_offset_minutes)


def station_local_now(station_tz: timezone) -> datetime:
    return datetime.now(timezone.utc).astimezone(station_tz)


def local_date_ymd_for_station(station_tz: timezone) -> str:
    dt = station_local_now(station_tz)
    return f"{dt.year:04d}-{dt.month}-{dt.day}"


def local_date_compact_for_station(station_tz: timezone) -> str:
    dt = station_local_now(station_tz)
    return f"{dt.year:04d}{dt.month:02d}{dt.day:02d}"


def build_page_url(station_page_url: str, local_date_ymd: str) -> str:
    return f"{station_page_url}/date/{local_date_ymd}"


def format_time_text_from_any(obs_time_local: str, valid_time_gmt: int, station_tz: timezone) -> str:
    dt_local = parse_datetime_flexible(obs_time_local)
    if dt_local is not None:
        if dt_local.tzinfo is None:
            dt_local = dt_local.replace(tzinfo=station_tz)
        else:
            dt_local = dt_local.astimezone(station_tz)
        return format_display_time(dt_local)

    if valid_time_gmt:
        dt_utc = datetime.fromtimestamp(int(valid_time_gmt), tz=timezone.utc)
        return format_display_time(dt_utc.astimezone(station_tz))

    raise RuntimeError("No usable time fields found in observation.")


def build_api_url(location_id: str, compact_date: str, stream_unit: str) -> str:
    stream_unit = normalize_unit(stream_unit)
    params = {
        "apiKey": API_KEY,
        "units": STREAM_TO_UNITS[stream_unit],
        "startDate": compact_date,
        "endDate": compact_date,
    }
    return f"https://api.weather.com/v1/location/{location_id}/observations/historical.json?{urlencode(params)}"


def build_datetime_api_url(geocode: str) -> str:
    params = {
        "apiKey": API_KEY,
        "geocode": geocode,
        "format": "json",
    }
    return f"https://api.weather.com/v3/dateTime?{urlencode(params)}"


def extract_condition_from_observation(obs: Dict[str, Any]) -> str:
    candidates = [
        obs.get("wxPhraseLong"),
        obs.get("phrase_32char"),
        obs.get("phrase_22char"),
        obs.get("condition"),
    ]
    for item in candidates:
        s = str(item or "").strip()
        if s:
            return s
    return ""


def extract_temp_from_observation(obs: Dict[str, Any], stream_unit: str) -> Decimal:
    if stream_unit == "C":
        metric = obs.get("metric")
        if isinstance(metric, dict) and metric.get("temp") is not None:
            return parse_decimal(metric.get("temp"), "metric.temp")
        if obs.get("temp") is not None:
            return parse_decimal(obs.get("temp"), "temp")
        raise RuntimeError("No metric temperature found in observation.")

    if stream_unit == "F":
        imperial = obs.get("imperial")
        if isinstance(imperial, dict) and imperial.get("temp") is not None:
            return parse_decimal(imperial.get("temp"), "imperial.temp")
        if obs.get("temp") is not None:
            return parse_decimal(obs.get("temp"), "temp")
        raise RuntimeError("No imperial temperature found in observation.")

    raise RuntimeError(f"Unsupported stream unit: {stream_unit!r}")


def extract_observations_array(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    arr = payload.get("observations")
    if isinstance(arr, list):
        return [x for x in arr if isinstance(x, dict)]
    raise RuntimeError("observations array not found in payload")


def parse_station_spec_from_url(page_url: str) -> ParsedStationSpec:
    s = str(page_url or "").strip().rstrip("/")

    m = re.search(
        r"^https?://www\.wunderground\.com/history/daily/([a-z]{2})/(.+?)/([^/]+?)(?:/date/(\d{4})-(\d{1,2})-(\d{1,2}))?$",
        s,
        re.IGNORECASE,
    )
    if not m:
        raise RuntimeError(f"Unsupported Wunderground history URL: {page_url!r}")

    country_code = m.group(1).lower()
    place_path = m.group(2).strip("/")
    station_code = m.group(3).upper()
    station_page_url = f"https://www.wunderground.com/history/daily/{country_code}/{place_path}/{station_code}"

    place_slug = place_path.replace("/", ":")
    name_base = f"{country_code}:{place_slug}:{station_code}"
    guessed_location_id = f"{station_code}:9:{country_code.upper()}"

    return ParsedStationSpec(
        name_base=name_base,
        station_page_url=station_page_url,
        country_code=country_code,
        place_slug=place_slug,
        station_code=station_code,
        guessed_location_id=guessed_location_id,
    )


def extract_page_details_from_text(text: str) -> Tuple[str, str]:
    location_id = ""
    geocode = ""

    patterns_location = [
        r"/v1/location/([A-Z0-9]+:\d+:[A-Z]{2})/observations/historical\.json",
        r'"locationId"\s*:\s*"([A-Z0-9]+:\d+:[A-Z]{2})"',
        r"location/([A-Z0-9]+:\d+:[A-Z]{2})/almanac/daily\.json",
    ]
    patterns_geocode = [
        r"geocode=([-0-9\.]+)%2C([-0-9\.]+)",
        r'"geocode"\s*:\s*"([-0-9\.]+),([-0-9\.]+)"',
    ]

    decoded = unquote(text)

    for pattern in patterns_location:
        m = re.search(pattern, decoded, re.IGNORECASE)
        if m:
            found = str(m.group(1) or "").strip()
            if found:
                location_id = found
                break

    for pattern in patterns_geocode:
        m = re.search(pattern, decoded, re.IGNORECASE)
        if m:
            lat = str(m.group(1) or "").strip()
            lon = str(m.group(2) or "").strip()
            if lat and lon:
                geocode = f"{lat},{lon}"
                break

    return location_id, geocode


def discover_page_details(session: requests.Session, spec: ParsedStationSpec) -> Tuple[str, str]:
    location_id = spec.guessed_location_id
    geocode = ""

    today_utc = datetime.now(timezone.utc)
    date_ymd = f"{today_utc.year:04d}-{today_utc.month}-{today_utc.day}"

    candidate_urls = [
        f"{spec.station_page_url}?unit=metric",
        f"{spec.station_page_url}?unit=us",
        spec.station_page_url,
        f"{spec.station_page_url}/date/{date_ymd}?unit=metric",
        f"{spec.station_page_url}/date/{date_ymd}?unit=us",
        f"{spec.station_page_url}/date/{date_ymd}",
    ]

    for url in candidate_urls:
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            found_location_id, found_geocode = extract_page_details_from_text(response.text)
            if found_location_id:
                location_id = found_location_id
            if found_geocode:
                geocode = found_geocode
            if location_id and geocode:
                break
        except Exception:
            continue

    return location_id, geocode


def find_first_value_deep(obj: Any, wanted_keys: List[str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key) in wanted_keys:
                return value
        for value in obj.values():
            found = find_first_value_deep(value, wanted_keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_value_deep(item, wanted_keys)
            if found is not None:
                return found
    return None


def discover_station_timezone_info(session: requests.Session, geocode: str) -> Tuple[str, int]:
    if not geocode:
        return "", 0

    url = build_datetime_api_url(geocode)
    try:
        response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return "", 0

    tz_name_raw = find_first_value_deep(
        payload,
        ["ianaTimeZone", "ianaTimeZoneName", "timeZone", "timezone", "olsonTimeZone", "tz"],
    )
    tz_name = str(tz_name_raw or "").strip()
    if "/" not in tz_name:
        tz_name = ""

    offset_raw = find_first_value_deep(
        payload,
        ["utcOffsetMinutes", "gmtOffsetMinutes", "offsetMinutes", "utcoffsetMinutes", "utcOffset", "gmtOffset", "offset"],
    )

    offset_minutes = 0
    if offset_raw is not None:
        try:
            if isinstance(offset_raw, str) and ":" in offset_raw:
                sign = -1 if offset_raw.strip().startswith("-") else 1
                hh, mm = offset_raw.strip().lstrip("+-").split(":", 1)
                offset_minutes = sign * (int(hh) * 60 + int(mm))
            else:
                value = float(offset_raw)
                if abs(value) <= 24:
                    offset_minutes = int(round(value * 60))
                else:
                    offset_minutes = int(round(value))
        except Exception:
            offset_minutes = 0

    return tz_name, offset_minutes


def load_json_file(path_str: str) -> Dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json_file(path_str: str, obj: Dict[str, Any]) -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_discovery_cache() -> Dict[str, DiscoveryInfo]:
    raw = load_json_file(DISCOVERY_CACHE_PATH)
    out: Dict[str, DiscoveryInfo] = {}
    for station_url, item in raw.items():
        if not isinstance(item, dict):
            continue
        try:
            out[str(station_url)] = DiscoveryInfo(
                location_id=str(item.get("location_id") or ""),
                guessed_location_id=str(item.get("guessed_location_id") or ""),
                geocode=str(item.get("geocode") or ""),
                timezone_name=str(item.get("timezone_name") or ""),
                timezone_offset_minutes=int(item.get("timezone_offset_minutes") or 0),
            )
        except Exception:
            continue
    return out


def save_discovery_cache(cache: Dict[str, DiscoveryInfo]) -> None:
    payload: Dict[str, Any] = {}
    for key, item in cache.items():
        payload[key] = asdict(item)
    save_json_file(DISCOVERY_CACHE_PATH, payload)


def load_runtime_state() -> Dict[str, Dict[str, Any]]:
    raw = load_json_file(STATE_CACHE_PATH)
    out: Dict[str, Dict[str, Any]] = {}
    for key, item in raw.items():
        if isinstance(item, dict):
            out[str(key)] = item
    return out


def save_runtime_state(state: MonitorState) -> None:
    payload: Dict[str, Any] = {}
    for key, target in state.targets.items():
        payload[key] = {
            "local_date_ymd": target.local_date_ymd,
            "page_url": target.page_url,
            "compact_date": target.compact_date,
            "api_url": target.api_url,
            "last_identity": target.last_identity,
            "last_value_text": target.last_value_text,
            "last_time_text": target.last_time_text,
            "last_valid_time_gmt": target.last_valid_time_gmt,
            "seen_rows": target.seen_rows,
            "change_count": target.change_count,
            "error_count": target.error_count,
            "no_data_count": target.no_data_count,
            "fail_streak": target.fail_streak,
            "next_poll_epoch": target.next_poll_epoch,
            "is_disabled": target.is_disabled,
            "disabled_reason": target.disabled_reason,
            "last_ok_wall": target.last_ok_wall,
            "last_err_wall": target.last_err_wall,
            "last_no_data_wall": target.last_no_data_wall,
        }
    save_json_file(STATE_CACHE_PATH, payload)


def restore_target_runtime_fields(target: TargetState, saved: Dict[str, Any]) -> None:
    target.local_date_ymd = str(saved.get("local_date_ymd") or "")
    target.page_url = str(saved.get("page_url") or "")
    target.compact_date = str(saved.get("compact_date") or "")
    target.api_url = str(saved.get("api_url") or "")
    target.last_identity = saved.get("last_identity")
    target.last_value_text = str(saved.get("last_value_text") or "")
    target.last_time_text = str(saved.get("last_time_text") or "")
    target.last_valid_time_gmt = int(saved.get("last_valid_time_gmt") or 0)
    target.seen_rows = int(saved.get("seen_rows") or 0)
    target.change_count = int(saved.get("change_count") or 0)
    target.error_count = int(saved.get("error_count") or 0)
    target.no_data_count = int(saved.get("no_data_count") or 0)
    target.fail_streak = int(saved.get("fail_streak") or 0)
    target.next_poll_epoch = float(saved.get("next_poll_epoch") or 0.0)
    target.is_disabled = bool(saved.get("is_disabled") or False)
    target.disabled_reason = str(saved.get("disabled_reason") or "")
    target.last_ok_wall = str(saved.get("last_ok_wall") or "")
    target.last_err_wall = str(saved.get("last_err_wall") or "")
    target.last_no_data_wall = str(saved.get("last_no_data_wall") or "")


def apply_station_local_date(target: TargetState, reset_on_change: bool) -> None:
    station_tz = choose_station_timezone(target.timezone_name, target.timezone_offset_minutes)
    new_local_date_ymd = local_date_ymd_for_station(station_tz)
    new_compact_date = local_date_compact_for_station(station_tz)
    new_page_url = build_page_url(target.station_page_url, new_local_date_ymd)
    new_api_url = build_api_url(target.location_id, new_compact_date, target.stream_unit)

    if not target.local_date_ymd:
        target.local_date_ymd = new_local_date_ymd
        target.compact_date = new_compact_date
        target.page_url = new_page_url
        target.api_url = new_api_url
        return

    if target.local_date_ymd != new_local_date_ymd:
        old_page_url = target.page_url
        old_date = target.local_date_ymd
        target.local_date_ymd = new_local_date_ymd
        target.compact_date = new_compact_date
        target.page_url = new_page_url
        target.api_url = new_api_url
        if reset_on_change:
            target.last_identity = None
            target.last_value_text = ""
            target.last_time_text = ""
            target.seen_rows = 0
            target.fail_streak = 0
        log(
            "DATE_ROLLOVER",
            f"target={target.name} old_date={old_date} new_date={new_local_date_ymd} old_page={old_page_url} new_page={new_page_url}",
            {
                "target": target.name,
                "old_date": old_date,
                "new_date": new_local_date_ymd,
                "old_page_url": old_page_url,
                "new_page_url": new_page_url,
                "stream_unit": target.stream_unit,
            },
        )
        return

    target.compact_date = new_compact_date
    target.page_url = new_page_url
    target.api_url = new_api_url


# ============================================================
# RATE LIMITER
# ============================================================
class RateLimiter:
    def __init__(self, max_per_second: float) -> None:
        self.max_per_second = max_per_second
        self.min_interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self.lock = Lock()
        self.last_ts = 0.0

    def wait_turn(self) -> None:
        if self.min_interval <= 0:
            return
        with self.lock:
            now = time.time()
            wait_seconds = self.min_interval - (now - self.last_ts)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            if REQUEST_JITTER_SECONDS > 0:
                time.sleep(random.uniform(0.0, REQUEST_JITTER_SECONDS))
            self.last_ts = time.time()


# ============================================================
# HTTP / API
# ============================================================
def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return s


def repair_location_id_if_needed(session: requests.Session, target: TargetState, rate_limiter: RateLimiter) -> bool:
    candidate_urls = [
        f"{target.page_url}?unit=metric",
        f"{target.page_url}?unit=us",
        f"{target.station_page_url}?unit=metric",
        f"{target.station_page_url}?unit=us",
        target.station_page_url,
    ]

    old_location_id = target.location_id
    new_location_id = old_location_id
    new_geocode = target.geocode

    for url in candidate_urls:
        try:
            rate_limiter.wait_turn()
            response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            found_location_id, found_geocode = extract_page_details_from_text(response.text)
            if found_location_id:
                new_location_id = found_location_id
            if found_geocode:
                new_geocode = found_geocode
            if new_location_id and new_location_id != old_location_id:
                break
        except Exception:
            continue

    if not new_location_id or new_location_id == old_location_id:
        return False

    target.location_id = new_location_id
    if new_geocode:
        target.geocode = new_geocode
        try:
            rate_limiter.wait_turn()
            tz_name, tz_offset = discover_station_timezone_info(session, new_geocode)
            if tz_name:
                target.timezone_name = tz_name
            target.timezone_offset_minutes = tz_offset
        except Exception:
            pass

    apply_station_local_date(target, reset_on_change=False)
    log(
        "LOCATION_REPAIRED",
        f"target={target.name} old_location_id={old_location_id} new_location_id={new_location_id} page={target.page_url}",
        {
            "target": target.name,
            "old_location_id": old_location_id,
            "new_location_id": new_location_id,
            "page_url": target.page_url,
            "station_page_url": target.station_page_url,
            "geocode": target.geocode,
            "timezone_name": target.timezone_name,
            "timezone_offset_minutes": int(target.timezone_offset_minutes or 0),
        },
    )
    return True


def fetch_last_truth_row(session: requests.Session, target: TargetState, rate_limiter: RateLimiter) -> TruthRow:
    apply_station_local_date(target, reset_on_change=False)

    for attempt in range(2):
        rate_limiter.wait_turn()
        response = session.get(target.api_url, timeout=HTTP_TIMEOUT_SECONDS)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = getattr(response, "status_code", None)
            if status_code == 400 and attempt == 0:
                repaired = repair_location_id_if_needed(session, target, rate_limiter)
                if repaired:
                    continue
                target.is_disabled = True
                target.disabled_reason = f"HTTP 400 and location_id could not be repaired from page: {target.location_id}"
                log(
                    "TARGET_DISABLED",
                    f"target={target.name} page={target.page_url} reason={target.disabled_reason}",
                    {
                        "target": target.name,
                        "page_url": target.page_url,
                        "station_page_url": target.station_page_url,
                        "location_id": target.location_id,
                        "disabled_reason": target.disabled_reason,
                    },
                )
            raise exc

        payload = response.json()
        observations = extract_observations_array(payload)
        if not observations:
            raise NoDataRecordedError("No observations in historical API response.")

        last = observations[-1]
        value_raw = extract_temp_from_observation(last, target.stream_unit)

        valid_time_gmt_raw = last.get("valid_time_gmt") if last.get("valid_time_gmt") is not None else last.get("expire_time_gmt")
        if valid_time_gmt_raw is None:
            valid_time_gmt_raw = last.get("epoch")

        valid_time_gmt = int(valid_time_gmt_raw) if valid_time_gmt_raw is not None else 0

        obs_time_local = str(last.get("obsTimeLocal") or last.get("observationTime") or "")
        obs_time_utc = str(last.get("obsTimeUtc") or last.get("utcTime") or "")

        station_tz = choose_station_timezone(target.timezone_name, target.timezone_offset_minutes)

        if not obs_time_local and valid_time_gmt:
            obs_time_local = datetime.fromtimestamp(valid_time_gmt, tz=timezone.utc).astimezone(station_tz).isoformat()

        if not obs_time_utc and valid_time_gmt:
            obs_time_utc = datetime.fromtimestamp(valid_time_gmt, tz=timezone.utc).isoformat()

        if not obs_time_local and not valid_time_gmt:
            raise RuntimeError(f"No usable time fields in observation. keys={sorted(last.keys())}")

        time_text = format_time_text_from_any(obs_time_local, valid_time_gmt, station_tz)
        condition_text = extract_condition_from_observation(last)

        return TruthRow(
            target_name=target.name,
            page_url=target.page_url,
            local_date_ymd=target.local_date_ymd,
            stream_unit=target.stream_unit,
            time_text=time_text,
            value_raw=value_raw,
            row_index=len(observations) - 1,
            row_count=len(observations),
            condition_text=condition_text,
            source_mode="api_historical_json",
            api_url=target.api_url,
            obs_time_local=obs_time_local,
            obs_time_utc=obs_time_utc,
            valid_time_gmt=valid_time_gmt,
            raw_json=last,
        )

    raise RuntimeError("Unreachable fetch_last_truth_row state")


# ============================================================
# MONITOR LOGIC
# ============================================================
def make_target_key(base_name: str, stream_unit: str) -> str:
    return f"{stream_unit}|{base_name}"


def compute_backoff_seconds(fail_streak: int, base_seconds: float, max_seconds: float) -> float:
    if fail_streak <= 0:
        return CHECK_INTERVAL_SECONDS
    seconds = base_seconds * (2 ** max(0, fail_streak - 1))
    return min(seconds, max_seconds)


def discover_or_load_station_info(
    session: requests.Session,
    spec: ParsedStationSpec,
    cache: Dict[str, DiscoveryInfo],
    rate_limiter: RateLimiter,
) -> DiscoveryInfo:
    existing = cache.get(spec.station_page_url)
    if existing and existing.location_id and existing.geocode and existing.timezone_name:
        return existing

    rate_limiter.wait_turn()
    location_id, geocode = discover_page_details(session, spec)
    timezone_name = ""
    timezone_offset_minutes = 0
    if geocode:
        rate_limiter.wait_turn()
        timezone_name, timezone_offset_minutes = discover_station_timezone_info(session, geocode)

    if existing:
        info = DiscoveryInfo(
            location_id=location_id or existing.location_id or spec.guessed_location_id,
            guessed_location_id=existing.guessed_location_id or spec.guessed_location_id,
            geocode=geocode or existing.geocode,
            timezone_name=timezone_name or existing.timezone_name,
            timezone_offset_minutes=(
                timezone_offset_minutes if timezone_name or timezone_offset_minutes else existing.timezone_offset_minutes
            ),
        )
    else:
        info = DiscoveryInfo(
            location_id=location_id or spec.guessed_location_id,
            guessed_location_id=spec.guessed_location_id,
            geocode=geocode,
            timezone_name=timezone_name,
            timezone_offset_minutes=timezone_offset_minutes,
        )

    cache[spec.station_page_url] = info
    return info


def register_targets(session: requests.Session, rate_limiter: RateLimiter) -> MonitorState:
    discovery_cache = load_discovery_cache()
    runtime_cache = load_runtime_state()
    state = MonitorState()
    now_epoch = time.time()

    for station_url in TARGET_STATION_URLS:
        spec = parse_station_spec_from_url(station_url)
        info = discover_or_load_station_info(session, spec, discovery_cache, rate_limiter)

        log(
            "CFG_DISCOVERY",
            f"station={spec.station_page_url} location_id={info.location_id} geocode={info.geocode or 'n/a'} tz={info.timezone_name or info.timezone_offset_minutes}",
        )

        for stream_unit in ("F", "C"):
            key = make_target_key(spec.name_base, stream_unit)
            if key in state.targets:
                raise RuntimeError(f"Duplicate target key: {key}")

            target = TargetState(
                name=f"{spec.name_base}:{stream_unit}",
                station_page_url=spec.station_page_url,
                station_code=spec.station_code,
                location_id=info.location_id,
                guessed_location_id=info.guessed_location_id,
                geocode=info.geocode,
                timezone_name=info.timezone_name,
                timezone_offset_minutes=info.timezone_offset_minutes,
                stream_unit=stream_unit,
                next_poll_epoch=now_epoch,
            )

            saved = runtime_cache.get(key)
            if saved:
                restore_target_runtime_fields(target, saved)
                if target.next_poll_epoch <= 0:
                    target.next_poll_epoch = now_epoch

            apply_station_local_date(target, reset_on_change=False)
            state.targets[key] = target

    save_discovery_cache(discovery_cache)
    state.last_heartbeat_epoch = now_epoch
    return state


def process_truth_row(target: TargetState, row: TruthRow) -> None:
    identity = truth_identity(row)
    target.seen_rows = row.row_count
    target.last_ok_wall = LOGGER.now_str()
    target.fail_streak = 0
    target.next_poll_epoch = time.time() + CHECK_INTERVAL_SECONDS

    if identity == target.last_identity:
        return

    if row.valid_time_gmt and target.last_valid_time_gmt and row.valid_time_gmt < target.last_valid_time_gmt:
        return

    row.first_seen_epoch = time.time()
    row.first_seen_wall = LOGGER.now_str()

    prev_identity = target.last_identity
    previous_value_text = target.last_value_text
    previous_value_key = decimal_key_text(previous_value_text) if previous_value_text else ""
    new_value_key = fmt_decimal(row.value_raw, 6)

    if prev_identity is not None and previous_value_key == new_value_key:
        target.last_identity = identity
        target.last_time_text = row.time_text
        if row.valid_time_gmt and row.valid_time_gmt > target.last_valid_time_gmt:
            target.last_valid_time_gmt = row.valid_time_gmt
        return

    target.last_identity = identity
    target.last_value_text = str(row.value_raw)
    target.last_time_text = row.time_text
    if row.valid_time_gmt and row.valid_time_gmt > target.last_valid_time_gmt:
        target.last_valid_time_gmt = row.valid_time_gmt
    target.change_count += 1

    tag = "TRUTH_INIT" if prev_identity is None else "TEMP_CHANGE"
    log(
        tag,
        f"target={target.name} page={row.page_url} {row.time_text} {fmt_decimal(row.value_raw, 1)}°{row.stream_unit}",
        {
            "target": target.name,
            "station_page_url": target.station_page_url,
            "page_url": row.page_url,
            "local_date_ymd": row.local_date_ymd,
            "location_id": target.location_id,
            "geocode": target.geocode,
            "timezone_name": target.timezone_name,
            "timezone_offset_minutes": int(target.timezone_offset_minutes or 0),
            "api_url": row.api_url,
            "stream_unit": row.stream_unit,
            "time_text": row.time_text,
            "value_raw": fmt_decimal(row.value_raw, 6),
            "previous_value": previous_value_text,
            "row_index": row.row_index,
            "row_count": row.row_count,
            "condition_text": row.condition_text,
            "valid_time_gmt": row.valid_time_gmt,
            "obs_time_local": row.obs_time_local,
            "obs_time_utc": row.obs_time_utc,
            "truth_identity": identity,
            "previous_identity": prev_identity,
            "source_mode": row.source_mode,
        },
    )


def mark_no_data(target: TargetState, exc: Exception) -> None:
    target.no_data_count += 1
    target.last_no_data_wall = LOGGER.now_str()
    target.fail_streak += 1
    target.next_poll_epoch = time.time() + max(NO_DATA_COOLDOWN_SECONDS, compute_backoff_seconds(target.fail_streak, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS))
    log(
        "NO_DATA",
        f"target={target.name} page={target.page_url} {exc}",
        {
            "target": target.name,
            "station_page_url": target.station_page_url,
            "page_url": target.page_url,
            "local_date_ymd": target.local_date_ymd,
            "location_id": target.location_id,
            "geocode": target.geocode,
            "api_url": target.api_url,
            "stream_unit": target.stream_unit,
            "message": str(exc),
            "fail_streak": target.fail_streak,
            "next_poll_epoch": target.next_poll_epoch,
        },
    )


def mark_error(target: TargetState, exc: Exception) -> None:
    target.error_count += 1
    target.last_err_wall = LOGGER.now_str()
    target.fail_streak += 1
    target.next_poll_epoch = time.time() + max(ERROR_COOLDOWN_SECONDS, compute_backoff_seconds(target.fail_streak, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS))
    log(
        "ERR",
        f"target={target.name} page={target.page_url} {type(exc).__name__}: {exc}",
        {
            "target": target.name,
            "station_page_url": target.station_page_url,
            "page_url": target.page_url,
            "local_date_ymd": target.local_date_ymd,
            "location_id": target.location_id,
            "geocode": target.geocode,
            "api_url": target.api_url,
            "stream_unit": target.stream_unit,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "fail_streak": target.fail_streak,
            "next_poll_epoch": target.next_poll_epoch,
        },
    )


def log_heartbeat(state: MonitorState) -> None:
    ready_count = 0
    disabled_count = 0
    now_epoch = time.time()
    sample_parts: List[str] = []
    for target in state.targets.values():
        if target.is_disabled:
            disabled_count += 1
            continue
        if target.next_poll_epoch <= now_epoch:
            ready_count += 1
        if len(sample_parts) < 6:
            last_value_text = "n/a"
            if target.last_value_text:
                last_value_text = f"{decimal_key_text(target.last_value_text)}°{target.stream_unit}@{target.last_time_text or '?'}"
            sample_parts.append(f"{target.name}[{target.local_date_ymd}]={last_value_text}")
    log("HEARTBEAT", f"poll={state.poll_count} targets={len(state.targets)} ready={ready_count} disabled={disabled_count} | " + " | ".join(sample_parts))


def due_target_keys(state: MonitorState) -> List[str]:
    now_epoch = time.time()
    keys = [key for key, target in state.targets.items() if (not target.is_disabled) and target.next_poll_epoch <= now_epoch]
    keys.sort(key=lambda k: state.targets[k].next_poll_epoch)
    return keys


def poll_one_target(target: TargetState, rate_limiter: RateLimiter) -> TruthRow:
    session = build_session()
    try:
        return fetch_last_truth_row(session, target, rate_limiter)
    finally:
        try:
            session.close()
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    if not API_KEY.strip():
        raise RuntimeError("API_KEY is empty.")

    if not TARGET_STATION_URLS:
        raise RuntimeError("TARGET_STATION_URLS is empty.")

    bootstrap_session = build_session()
    rate_limiter = RateLimiter(MAX_REQUESTS_PER_SECOND)
    state = register_targets(bootstrap_session, rate_limiter)
    try:
        bootstrap_session.close()
    except Exception:
        pass

    log(
        "CFG",
        (
            f"TARGET_STATION_COUNT={len(TARGET_STATION_URLS)} TARGET_STREAM_COUNT={len(state.targets)} "
            f"CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS} MAX_WORKERS={MAX_WORKERS} "
            f"MAX_REQUESTS_PER_SECOND={MAX_REQUESTS_PER_SECOND}"
        ),
    )

    in_flight: Dict[Any, str] = {}

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            while True:
                state.poll_count += 1
                now_epoch = time.time()

                for target in state.targets.values():
                    apply_station_local_date(target, reset_on_change=True)

                for key in due_target_keys(state):
                    if key in in_flight.values():
                        continue
                    if len(in_flight) >= MAX_WORKERS:
                        break
                    target = state.targets[key]
                    future = executor.submit(poll_one_target, target, rate_limiter)
                    in_flight[future] = key
                    target.next_poll_epoch = now_epoch + HTTP_TIMEOUT_SECONDS

                if in_flight:
                    done, _ = wait(list(in_flight.keys()), timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        key = in_flight.pop(future)
                        target = state.targets[key]
                        try:
                            row = future.result()
                            process_truth_row(target, row)
                        except NoDataRecordedError as exc:
                            mark_no_data(target, exc)
                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            mark_error(target, exc)
                else:
                    time.sleep(0.5)

                if state.poll_count % SAVE_STATE_EVERY_POLLS == 0:
                    save_runtime_state(state)

                if (time.time() - state.last_heartbeat_epoch) >= HEARTBEAT_EVERY_SECONDS:
                    log_heartbeat(state)
                    state.last_heartbeat_epoch = time.time()

    except KeyboardInterrupt:
        save_runtime_state(state)
        log("STOP", "Stopped by user.")
        print()
        input("Press Enter to exit...")
        return 0
    finally:
        save_runtime_state(state)


if __name__ == "__main__":
    try:
        rc = main()
        print()
        input("Press Enter to exit...")
        raise SystemExit(rc)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[FATAL] {type(exc).__name__}: {exc}", flush=True)
        print()
        input("Press Enter to exit...")
        raise
