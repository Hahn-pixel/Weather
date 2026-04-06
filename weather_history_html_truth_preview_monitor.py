#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

try:
    import requests
except Exception as exc:
    print(f"[FATAL] Failed to import requests: {exc}", flush=True)
    print("[HINT] Run: py -m pip install requests", flush=True)
    print()
    input("Press Enter to exit...")
    raise

try:
    from win10toast import ToastNotifier  # type: ignore
except Exception:
    ToastNotifier = None  # type: ignore


# ============================================================
# CONFIG
# ============================================================
TARGET_PAGES = [
    "https://www.wunderground.com/history/daily/it/ferno/LIMC/date/2026-4-6",
    "https://www.wunderground.com/history/daily/us/ga/atlanta/KATL/date/2026-4-6",
    "https://www.wunderground.com/history/daily/de/oberding/EDDM/date/2026-4-6",
]

API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
RUN_MODE = "truth_plus_preview"  # preview_only | truth_only | truth_plus_preview
CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True

TRUTH_ARRAY_POINT_MODE = "latest"  # latest | previous | penultimate_nonempty

LOG_TO_FILE = False
LOG_FILE_PATH = "weather_history_html_truth_preview_monitor.log"
WRITE_JSONL_EVENTS = True
JSONL_FILE_PATH = "weather_history_html_truth_preview_monitor.jsonl"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
REFERER = "https://www.wunderground.com/"

MATCH_TOLERANCE_C = Decimal("0.001")
MATCH_TOLERANCE_F = Decimal("0.001")

ENABLE_DESKTOP_ALERT = True
ENABLE_BELL_ALERT = False
DESKTOP_ALERT_TITLE = "Weather Truth Change"
DESKTOP_ALERT_DURATION_SECONDS = 5

API_MODE_ORDER = ["v3", "v2", "v1"]
V2_V3_AUTH_COOLDOWN_SECONDS = 3600
ENABLE_V1_LOCATION_RESOLUTION = True
V1_MAX_LOCATION_CANDIDATES = 12
V1_TEST_LIMIT_PER_POLL = 8
V1_BAD_CANDIDATE_COOLDOWN_SECONDS = 1800
V1_ALLOW_HEURISTIC_CANDIDATES = True
LOG_CADENCE = False
CADENCE_TAIL_COUNT = 5


# ============================================================
# DATA MODELS
# ============================================================
@dataclass
class PreviewState:
    page_url: str
    location_id: str
    stream_unit: str
    value_raw: Decimal
    valid_time_gmt: int
    expire_time_gmt: int
    obs_name: str
    obs_id: str
    wx_phrase: str
    wdir_cardinal: Optional[str]
    wspd: Optional[Decimal]
    first_seen_epoch: float
    first_seen_wall: str
    source_mode: str


@dataclass
class TruthState:
    page_url: str
    location_id: str
    stream_unit: str
    value_raw: Decimal
    valid_time_gmt: int
    expire_time_gmt: int
    obs_name: str
    obs_id: str
    wx_phrase: str
    row_index: int
    row_count: int
    source_mode: str
    first_seen_epoch: float = 0.0
    first_seen_wall: str = ""


@dataclass
class PageContext:
    page_url: str
    station_id: str
    country_code: str
    location_id: str
    date_key: str


@dataclass
class MonitorMemory:
    poll_count: int = 0
    last_truth_identity_by_key: Dict[str, Optional[str]] = field(default_factory=dict)
    last_preview_identity_by_key: Dict[str, Optional[str]] = field(default_factory=dict)
    pending_preview_by_key: Dict[str, Dict[str, PreviewState]] = field(default_factory=dict)
    last_alerted_truth_identity_by_key: Dict[str, Optional[str]] = field(default_factory=dict)
    mode_skip_until: Dict[str, int] = field(default_factory=dict)
    v1_working_location_id: Dict[str, str] = field(default_factory=dict)
    v1_candidate_cache: Dict[str, List[str]] = field(default_factory=dict)
    v1_bad_candidate_until: Dict[str, int] = field(default_factory=dict)
    last_cadence_signature: Dict[str, Optional[str]] = field(default_factory=dict)


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


def maybe_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return Decimal(text)
    except Exception:
        return None


def fmt_decimal(value: Decimal, places: int = 3) -> str:
    q = Decimal("1").scaleb(-places)
    return f"{value.quantize(q):f}"


def decimal_key(value: Decimal) -> str:
    return fmt_decimal(value, 6)


def stream_tolerance(stream_unit: str) -> Decimal:
    return MATCH_TOLERANCE_C if stream_unit == "C" else MATCH_TOLERANCE_F


def derive_history_date_from_url(url: str) -> str:
    match = re.search(r"/date/(\d{4})-(\d{1,2})-(\d{1,2})(?:/|$)", url, re.IGNORECASE)
    if not match:
        raise RuntimeError(f"Cannot derive history date from URL: {url}")
    return f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"


def derive_station_and_country(url: str) -> tuple[str, str]:
    match = re.search(
        r"/history/daily/([a-z]{2})/.+?/([A-Z0-9]{3,8})/date/\d{4}-\d{1,2}-\d{1,2}(?:/|$)",
        url,
        re.IGNORECASE,
    )
    if not match:
        raise RuntimeError(f"Cannot derive station from URL: {url}")
    country = match.group(1).upper()
    station = match.group(2).upper()
    return station, country


def derive_location_id_from_url(url: str) -> str:
    station, country = derive_station_and_country(url)
    return f"{station}:9:{country}"


def epoch_to_utc_str(epoch_seconds: int) -> str:
    if epoch_seconds <= 0:
        return "n/a"
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def stream_key(page_url: str, stream_unit: str) -> str:
    return f"{page_url}|{stream_unit}"


def preview_identity(state: PreviewState) -> str:
    return f"{state.valid_time_gmt}|{decimal_key(state.value_raw)}"


def truth_identity(state: TruthState) -> str:
    return f"{state.valid_time_gmt}|{decimal_key(state.value_raw)}"


def preview_bucket_key(state: PreviewState) -> str:
    return f"{decimal_key(state.value_raw)}|{state.valid_time_gmt}"


def coerce_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text == "":
        return 0
    return int(float(text))


def utc_now_epoch() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def unique_keep_order(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def is_probably_postal_composite(location_id: str) -> bool:
    return bool(re.fullmatch(r"[^:]+:\d+:[A-Z]{2}", location_id or ""))


# ============================================================
# ALERTS
# ============================================================
def emit_truth_alert(state: TruthState) -> None:
    if ENABLE_DESKTOP_ALERT and ToastNotifier is not None:
        try:
            toaster = ToastNotifier()
            message = (
                f"{state.obs_name or state.location_id} | {state.stream_unit} {fmt_decimal(state.value_raw)}°{state.stream_unit} | "
                f"{epoch_to_utc_str(state.valid_time_gmt)}"
            )
            show_toast = getattr(toaster, "show_toast", None)
            if callable(show_toast):
                show_toast(
                    DESKTOP_ALERT_TITLE,
                    message,
                    duration=DESKTOP_ALERT_DURATION_SECONDS,
                    threaded=True,
                )
        except Exception as exc:
            log("ALERT_ERR", f"Desktop alert failed: {type(exc).__name__}: {exc}")

    if ENABLE_BELL_ALERT:
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass


# ============================================================
# HTTP / API
# ============================================================
def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,uk;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": REFERER,
            "DNT": "1",
        }
    )
    return session


def fetch_v3(session: requests.Session, station_id: str, units: str, date_key: str) -> Dict[str, Any]:
    response = session.get(
        "https://api.weather.com/v3/wx/observations/historical",
        params={
            "stationId": station_id,
            "date": date_key,
            "units": units,
            "format": "json",
            "apiKey": API_KEY,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
    response.raise_for_status()
    return response.json()


def fetch_v2(session: requests.Session, station_id: str, units: str, date_key: str) -> Dict[str, Any]:
    response = session.get(
        "https://api.weather.com/v2/pws/observations/all",
        params={
            "stationId": station_id,
            "date": date_key,
            "units": units,
            "format": "json",
            "numericPrecision": "decimal",
            "apiKey": API_KEY,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
    response.raise_for_status()
    return response.json()


def fetch_v1(session: requests.Session, location_id: str, units: str, date_key: str) -> Dict[str, Any]:
    response = session.get(
        f"https://api.weather.com/v1/location/{quote(location_id, safe='')}/observations/historical.json",
        params={
            "apiKey": API_KEY,
            "units": units,
            "startDate": date_key,
            "endDate": date_key,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
    response.raise_for_status()
    return response.json()


def fetch_location_point(session: requests.Session, query: str, query_type: str) -> Dict[str, Any]:
    response = session.get(
        "https://api.weather.com/v3/location/point",
        params={
            "query": query,
            "queryType": query_type,
            "language": "en-US",
            "format": "json",
            "apiKey": API_KEY,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
    response.raise_for_status()
    return response.json()


# ============================================================
# EXTRACTION HELPERS
# ============================================================
def dict_get_ci(dct: Dict[str, Any], *keys: str) -> Any:
    lowered = {str(k).lower(): v for k, v in dct.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "temp": dict_get_ci(row, "temp", "temperature"),
        "valid_time_gmt": dict_get_ci(row, "valid_time_gmt", "validTimeUtc", "epochTimeUtc", "epoch", "dateEpoch", "time"),
        "expire_time_gmt": dict_get_ci(row, "expire_time_gmt", "expireTimeUtc"),
        "obs_name": dict_get_ci(row, "obs_name", "obsName", "stationName"),
        "obs_id": dict_get_ci(row, "obs_id", "obsId", "stationId"),
        "wx_phrase": dict_get_ci(row, "wx_phrase", "iconPhrase", "phrase", "condition"),
        "wdir_cardinal": dict_get_ci(row, "wdir_cardinal"),
        "wspd": dict_get_ci(row, "wspd", "windSpeed"),
    }


def flatten_parallel_observations(obs_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    temp_key = None
    time_key = None
    expire_key = None
    phrase_key = None
    obs_name_key = None

    for key, value in obs_obj.items():
        if not isinstance(value, list):
            continue
        kl = str(key).lower()
        if temp_key is None and kl in {"temperature", "temp"}:
            temp_key = key
        if time_key is None and kl in {"validtimeutc", "valid_time_gmt", "epochtimeutc", "time"}:
            time_key = key
        if expire_key is None and kl in {"expiretimeutc", "expire_time_gmt"}:
            expire_key = key
        if phrase_key is None and kl in {"iconphrase", "wx_phrase", "phrase", "condition"}:
            phrase_key = key
        if obs_name_key is None and kl in {"obsname", "obs_name", "stationname"}:
            obs_name_key = key

    if temp_key is None:
        return []

    temps = obs_obj.get(temp_key)
    if not isinstance(temps, list) or not temps:
        return []

    times = obs_obj.get(time_key) if time_key else []
    expires = obs_obj.get(expire_key) if expire_key else []
    phrases = obs_obj.get(phrase_key) if phrase_key else []
    names = obs_obj.get(obs_name_key) if obs_name_key else []

    rows: List[Dict[str, Any]] = []
    for i in range(len(temps)):
        rows.append(
            normalize_row(
                {
                    "temp": temps[i],
                    "valid_time_gmt": times[i] if isinstance(times, list) and i < len(times) else None,
                    "expire_time_gmt": expires[i] if isinstance(expires, list) and i < len(expires) else None,
                    "wx_phrase": phrases[i] if isinstance(phrases, list) and i < len(phrases) else None,
                    "obs_name": names[i] if isinstance(names, list) and i < len(names) else None,
                }
            )
        )
    return rows


def extract_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[List[Dict[str, Any]]] = []

    top_obs = dict_get_ci(payload, "observations")
    if isinstance(top_obs, list) and top_obs and all(isinstance(x, dict) for x in top_obs):
        candidates.append([normalize_row(x) for x in top_obs])
    elif isinstance(top_obs, dict):
        flat = flatten_parallel_observations(top_obs)
        if flat:
            candidates.append(flat)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            obs = dict_get_ci(node, "observations")
            if isinstance(obs, list) and obs and all(isinstance(x, dict) for x in obs):
                candidates.append([normalize_row(x) for x in obs])
            elif isinstance(obs, dict):
                flat = flatten_parallel_observations(obs)
                if flat:
                    candidates.append(flat)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                rows = [normalize_row(x) for x in node]
                if any(maybe_decimal(r.get("temp")) is not None for r in rows):
                    candidates.append(rows)
            for value in node:
                walk(value)

    walk(payload)

    best: List[Dict[str, Any]] = []
    for rows in candidates:
        usable = [r for r in rows if maybe_decimal(r.get("temp")) is not None]
        if len(usable) > len(best):
            best = usable
    return best


def select_observation(observations: List[Dict[str, Any]], mode: str) -> tuple[int, Dict[str, Any]]:
    if not observations:
        raise RuntimeError("Cannot select observation from empty observations array.")

    if mode == "latest":
        return len(observations) - 1, observations[-1]

    if mode == "previous":
        if len(observations) >= 2:
            return len(observations) - 2, observations[-2]
        return len(observations) - 1, observations[-1]

    if mode == "penultimate_nonempty":
        for idx in range(len(observations) - 1, -1, -1):
            item = observations[idx]
            if maybe_decimal(item.get("temp")) is not None:
                return idx, item
        return len(observations) - 1, observations[-1]

    raise RuntimeError(f"Unsupported TRUTH_ARRAY_POINT_MODE: {mode!r}")


# ============================================================
# LOCATION RESOLUTION
# ============================================================
def _extract_scalar_candidates(node: Any, out: List[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            kl = str(key).lower()
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    continue
                if kl in {"postalkey", "postal_key", "locationid", "location_id", "legacylocationid", "legacy_location_id", "locid", "loc_id", "id"}:
                    out.append(text)
                elif is_probably_postal_composite(text):
                    out.append(text)
            elif isinstance(value, list):
                if kl in {"postalkeys", "postal_keys", "locationids", "location_ids", "ids"}:
                    for item in value:
                        if isinstance(item, str):
                            out.append(item)
                for item in value:
                    _extract_scalar_candidates(item, out)
            else:
                _extract_scalar_candidates(value, out)
    elif isinstance(node, list):
        for item in node:
            _extract_scalar_candidates(item, out)


def heuristic_v1_candidates(page_ctx: PageContext) -> List[str]:
    if not V1_ALLOW_HEURISTIC_CANDIDATES:
        return []
    return unique_keep_order(
        [
            page_ctx.location_id,
            f"{page_ctx.station_id}:{page_ctx.country_code}",
            f"{page_ctx.station_id}:1:{page_ctx.country_code}",
            f"{page_ctx.station_id}:4:{page_ctx.country_code}",
            f"{page_ctx.station_id}:9:{page_ctx.country_code}",
        ]
    )


def resolve_v1_location_candidates(session: requests.Session, memory: MonitorMemory, page_ctx: PageContext) -> List[str]:
    cache_key = f"{page_ctx.station_id}|{page_ctx.country_code}"
    cached = memory.v1_candidate_cache.get(cache_key)
    if cached:
        return cached[:]

    candidates: List[str] = []
    if ENABLE_V1_LOCATION_RESOLUTION:
        queries: List[tuple[str, str]] = [(page_ctx.station_id, "icaoCode")]
        if 3 <= len(page_ctx.station_id) <= 4:
            queries.append((page_ctx.station_id[-3:], "iataCode"))
        queries.append((page_ctx.station_id, "locationId"))

        for query, query_type in queries:
            try:
                payload = fetch_location_point(session, query, query_type)
                raw_candidates: List[str] = []
                _extract_scalar_candidates(payload, raw_candidates)
                candidates.extend(unique_keep_order(raw_candidates))
            except Exception:
                continue

    working = memory.v1_working_location_id.get(cache_key)
    ordered: List[str] = []
    if working:
        ordered.append(working)

    postal_like = [x for x in candidates if is_probably_postal_composite(x)]
    non_postal = [x for x in candidates if x not in postal_like]
    ordered.extend(postal_like)
    ordered.extend(non_postal)
    ordered.extend(heuristic_v1_candidates(page_ctx))
    ordered = unique_keep_order(ordered)[:V1_MAX_LOCATION_CANDIDATES]
    memory.v1_candidate_cache[cache_key] = ordered[:]
    return ordered


def mode_skip_key(page_ctx: PageContext, units: str, mode: str) -> str:
    return f"{page_ctx.station_id}|{page_ctx.country_code}|{units}|{mode}"


def should_skip_mode(memory: MonitorMemory, page_ctx: PageContext, units: str, mode: str) -> bool:
    return utc_now_epoch() < memory.mode_skip_until.get(mode_skip_key(page_ctx, units, mode), 0)


def mark_mode_skip(memory: MonitorMemory, page_ctx: PageContext, units: str, mode: str, seconds: int) -> None:
    memory.mode_skip_until[mode_skip_key(page_ctx, units, mode)] = utc_now_epoch() + seconds


def should_skip_bad_candidate(memory: MonitorMemory, candidate_key: str) -> bool:
    return utc_now_epoch() < memory.v1_bad_candidate_until.get(candidate_key, 0)


def mark_bad_candidate(memory: MonitorMemory, candidate_key: str) -> None:
    memory.v1_bad_candidate_until[candidate_key] = utc_now_epoch() + V1_BAD_CANDIDATE_COOLDOWN_SECONDS


def mark_good_candidate(memory: MonitorMemory, page_ctx: PageContext, location_id: str) -> None:
    cache_key = f"{page_ctx.station_id}|{page_ctx.country_code}"
    memory.v1_working_location_id[cache_key] = location_id
    current = memory.v1_candidate_cache.get(cache_key, [])
    memory.v1_candidate_cache[cache_key] = unique_keep_order([location_id] + current)


# ============================================================
# FETCHERS
# ============================================================
def fetch_dense_rows(session: requests.Session, memory: MonitorMemory, page_ctx: PageContext, units: str) -> tuple[List[Dict[str, Any]], str]:
    last_error = ""

    for mode in API_MODE_ORDER:
        if mode in {"v2", "v3"} and should_skip_mode(memory, page_ctx, units, mode):
            continue
        try:
            if mode == "v3":
                payload = fetch_v3(session, page_ctx.station_id, units, page_ctx.date_key)
                rows = extract_rows(payload)
                if rows:
                    return rows, mode
                last_error = f"{mode}: HTTP 200 but no usable rows"
                continue

            if mode == "v2":
                payload = fetch_v2(session, page_ctx.station_id, units, page_ctx.date_key)
                rows = extract_rows(payload)
                if rows:
                    return rows, mode
                last_error = f"{mode}: HTTP 200 but no usable rows"
                continue

            if mode == "v1":
                cache_key = f"{page_ctx.station_id}|{page_ctx.country_code}"
                candidates = resolve_v1_location_candidates(session, memory, page_ctx)
                tested = 0
                for location_id in candidates:
                    candidate_key = f"{cache_key}|{location_id}|{units}"
                    if should_skip_bad_candidate(memory, candidate_key):
                        continue
                    tested += 1
                    if tested > V1_TEST_LIMIT_PER_POLL:
                        break
                    try:
                        payload = fetch_v1(session, location_id, units, page_ctx.date_key)
                        rows = extract_rows(payload)
                        if rows:
                            mark_good_candidate(memory, page_ctx, location_id)
                            return rows, f"v1[{location_id}]"
                        last_error = f"v1[{location_id}]: HTTP 200 but no usable rows"
                        mark_bad_candidate(memory, candidate_key)
                    except requests.HTTPError as exc:
                        status = exc.response.status_code if exc.response is not None else None
                        last_error = f"v1[{location_id}]: HTTP {status}: {exc}"
                        if status in {400, 404}:
                            mark_bad_candidate(memory, candidate_key)
                        continue
                    except Exception as exc:
                        last_error = f"v1[{location_id}]: {type(exc).__name__}: {exc}"
                        continue
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if mode in {"v2", "v3"} and status in {401, 403}:
                mark_mode_skip(memory, page_ctx, units, mode, V2_V3_AUTH_COOLDOWN_SECONDS)
                last_error = f"{mode} unauthorized ({status})"
                continue
            last_error = f"{mode}: HTTP {status}: {exc}"
            continue
        except Exception as exc:
            last_error = f"{mode}: {type(exc).__name__}: {exc}"
            continue

    raise RuntimeError(f"No usable rows for page={page_ctx.page_url} units={units}. Last error: {last_error}")


def maybe_log_cadence(memory: MonitorMemory, page_ctx: PageContext, stream_unit: str, rows: List[Dict[str, Any]], source_mode: str) -> None:
    if not LOG_CADENCE:
        return
    key = f"{page_ctx.page_url}|{stream_unit}|cadence"
    usable = [r for r in rows if int(r.get("valid_time_gmt") or 0) > 0]
    tail = usable[-CADENCE_TAIL_COUNT:] if len(usable) > CADENCE_TAIL_COUNT else usable[:]
    if len(tail) < 2:
        return
    times = [int(r.get("valid_time_gmt") or 0) for r in tail]
    deltas = [int((times[i] - times[i - 1]) / 60) for i in range(1, len(times))]
    signature = f"{source_mode}|{times}|{deltas}"
    if memory.last_cadence_signature.get(key) == signature:
        return
    memory.last_cadence_signature[key] = signature
    log(
        "CADENCE",
        f"page={page_ctx.page_url} stream={stream_unit} source_mode={source_mode} tail_times_utc={times} tail_deltas_min={deltas}",
    )


def extract_preview_state_from_rows(page_ctx: PageContext, rows: List[Dict[str, Any]], units: str, source_mode: str) -> PreviewState:
    if not rows:
        raise RuntimeError(f"No observations for preview units={units!r} on {page_ctx.page_url}")
    observation = rows[-1]
    metadata_expire = 0
    stream_unit = "F" if units.lower() == "e" else "C"
    return PreviewState(
        page_url=page_ctx.page_url,
        location_id=page_ctx.location_id,
        stream_unit=stream_unit,
        value_raw=parse_decimal(observation.get("temp"), f"preview.temp.{units}"),
        valid_time_gmt=coerce_int(observation.get("valid_time_gmt")),
        expire_time_gmt=coerce_int(observation.get("expire_time_gmt") or metadata_expire),
        obs_name=str(observation.get("obs_name") or ""),
        obs_id=str(observation.get("obs_id") or ""),
        wx_phrase=str(observation.get("wx_phrase") or ""),
        wdir_cardinal=str(observation.get("wdir_cardinal") or "") if observation.get("wdir_cardinal") is not None else None,
        wspd=maybe_decimal(observation.get("wspd")),
        first_seen_epoch=0.0,
        first_seen_wall="",
        source_mode=source_mode,
    )


def extract_truth_state_from_rows(page_ctx: PageContext, rows: List[Dict[str, Any]], units: str, point_mode: str, source_mode: str) -> TruthState:
    if not rows:
        raise RuntimeError(f"No observations for truth units={units!r} on {page_ctx.page_url}")
    row_index, observation = select_observation(rows, point_mode)
    stream_unit = "F" if units.lower() == "e" else "C"
    return TruthState(
        page_url=page_ctx.page_url,
        location_id=page_ctx.location_id,
        stream_unit=stream_unit,
        value_raw=parse_decimal(observation.get("temp"), f"truth.temp.{units}"),
        valid_time_gmt=coerce_int(observation.get("valid_time_gmt")),
        expire_time_gmt=coerce_int(observation.get("expire_time_gmt")),
        obs_name=str(observation.get("obs_name") or ""),
        obs_id=str(observation.get("obs_id") or ""),
        wx_phrase=str(observation.get("wx_phrase") or ""),
        row_index=row_index,
        row_count=len(rows),
        source_mode=f"{source_mode}:{point_mode}",
    )


def fetch_preview_states(session: requests.Session, memory: MonitorMemory, page_ctx: PageContext) -> Dict[str, PreviewState]:
    rows_c, mode_c = fetch_dense_rows(session, memory, page_ctx, "m")
    rows_f, mode_f = fetch_dense_rows(session, memory, page_ctx, "e")
    maybe_log_cadence(memory, page_ctx, "C", rows_c, mode_c)
    maybe_log_cadence(memory, page_ctx, "F", rows_f, mode_f)
    return {
        "C": extract_preview_state_from_rows(page_ctx, rows_c, "m", mode_c),
        "F": extract_preview_state_from_rows(page_ctx, rows_f, "e", mode_f),
    }


def fetch_truth_states(session: requests.Session, memory: MonitorMemory, page_ctx: PageContext, point_mode: str) -> Dict[str, TruthState]:
    rows_c, mode_c = fetch_dense_rows(session, memory, page_ctx, "m")
    rows_f, mode_f = fetch_dense_rows(session, memory, page_ctx, "e")
    maybe_log_cadence(memory, page_ctx, "C", rows_c, mode_c)
    maybe_log_cadence(memory, page_ctx, "F", rows_f, mode_f)
    return {
        "C": extract_truth_state_from_rows(page_ctx, rows_c, "m", point_mode, mode_c),
        "F": extract_truth_state_from_rows(page_ctx, rows_f, "e", point_mode, mode_f),
    }


# ============================================================
# MATCHER / PROCESSORS
# ============================================================
def process_preview_state(memory: MonitorMemory, state: PreviewState) -> None:
    key = stream_key(state.page_url, state.stream_unit)
    identity = preview_identity(state)
    if identity == memory.last_preview_identity_by_key.get(key):
        return

    state.first_seen_epoch = time.time()
    state.first_seen_wall = LOGGER.now_str()
    memory.last_preview_identity_by_key[key] = identity
    pending = memory.pending_preview_by_key.setdefault(key, {})
    pending[preview_bucket_key(state)] = state

    log(
        "PREVIEW_NEW",
        (
            f"page={state.page_url} stream={state.stream_unit} source_mode={state.source_mode} "
            f"value={fmt_decimal(state.value_raw)}°{state.stream_unit} "
            f"valid_time_gmt={state.valid_time_gmt} ({epoch_to_utc_str(state.valid_time_gmt)}) "
            f"expire_time_gmt={state.expire_time_gmt} ({epoch_to_utc_str(state.expire_time_gmt)})"
        ),
        {
            "page_url": state.page_url,
            "location_id": state.location_id,
            "stream_unit": state.stream_unit,
            "source_mode": state.source_mode,
            "value_raw": fmt_decimal(state.value_raw, 6),
            "valid_time_gmt": state.valid_time_gmt,
            "expire_time_gmt": state.expire_time_gmt,
            "obs_name": state.obs_name,
            "obs_id": state.obs_id,
            "wx_phrase": state.wx_phrase,
        },
    )


def process_truth_state(memory: MonitorMemory, state: TruthState) -> None:
    key = stream_key(state.page_url, state.stream_unit)
    identity = truth_identity(state)
    if identity == memory.last_truth_identity_by_key.get(key):
        return

    if memory.last_alerted_truth_identity_by_key.get(key) != identity:
        emit_truth_alert(state)
        memory.last_alerted_truth_identity_by_key[key] = identity

    state.first_seen_epoch = time.time()
    state.first_seen_wall = LOGGER.now_str()
    memory.last_truth_identity_by_key[key] = identity

    log(
        "TRUTH_NEW",
        (
            f"page={state.page_url} stream={state.stream_unit} source_mode={state.source_mode} "
            f"value={fmt_decimal(state.value_raw)}°{state.stream_unit} row_index={state.row_index} row_count={state.row_count} "
            f"valid_time_gmt={state.valid_time_gmt} ({epoch_to_utc_str(state.valid_time_gmt)})"
        ),
        {
            "page_url": state.page_url,
            "location_id": state.location_id,
            "stream_unit": state.stream_unit,
            "source_mode": state.source_mode,
            "value_raw": fmt_decimal(state.value_raw, 6),
            "row_index": state.row_index,
            "row_count": state.row_count,
            "valid_time_gmt": state.valid_time_gmt,
            "expire_time_gmt": state.expire_time_gmt,
            "obs_name": state.obs_name,
            "obs_id": state.obs_id,
            "wx_phrase": state.wx_phrase,
        },
    )

    evaluate_matches(memory, state)


def evaluate_matches(memory: MonitorMemory, truth_state: TruthState) -> None:
    key = stream_key(truth_state.page_url, truth_state.stream_unit)
    pending_map = memory.pending_preview_by_key.get(key, {})
    if not pending_map:
        return

    candidates = list(pending_map.values())
    if not candidates:
        return

    same_value_candidates = [
        item for item in candidates if abs(item.value_raw - truth_state.value_raw) <= stream_tolerance(truth_state.stream_unit)
    ]

    if same_value_candidates:
        match = max(same_value_candidates, key=lambda x: x.valid_time_gmt)
        lag_seconds = max(0.0, truth_state.first_seen_epoch - match.first_seen_epoch)

        log(
            "MATCH",
            (
                f"page={truth_state.page_url} stream={truth_state.stream_unit} source_mode={truth_state.source_mode} "
                f"truth_value={fmt_decimal(truth_state.value_raw)}°{truth_state.stream_unit} "
                f"truth_valid_time_gmt={truth_state.valid_time_gmt} preview_valid_time_gmt={match.valid_time_gmt} "
                f"lag_seconds={lag_seconds:.1f}"
            ),
            {
                "page_url": truth_state.page_url,
                "location_id": truth_state.location_id,
                "stream_unit": truth_state.stream_unit,
                "source_mode": truth_state.source_mode,
                "truth_value_raw": fmt_decimal(truth_state.value_raw, 6),
                "truth_valid_time_gmt": truth_state.valid_time_gmt,
                "preview_valid_time_gmt": match.valid_time_gmt,
                "preview_value_raw": fmt_decimal(match.value_raw, 6),
                "lag_seconds": round(lag_seconds, 3),
            },
        )

        to_delete: List[str] = []
        for bucket_key, item in pending_map.items():
            if abs(item.value_raw - truth_state.value_raw) <= stream_tolerance(truth_state.stream_unit) and item.valid_time_gmt <= match.valid_time_gmt:
                to_delete.append(bucket_key)
        for bucket_key in to_delete:
            pending_map.pop(bucket_key, None)
        return

    closest = min(candidates, key=lambda x: abs(x.value_raw - truth_state.value_raw))
    delta = abs(closest.value_raw - truth_state.value_raw)

    log(
        "DIFF",
        (
            f"page={truth_state.page_url} stream={truth_state.stream_unit} source_mode={truth_state.source_mode} "
            f"truth_value={fmt_decimal(truth_state.value_raw)}°{truth_state.stream_unit} truth_valid_time_gmt={truth_state.valid_time_gmt} "
            f"closest_preview_value={fmt_decimal(closest.value_raw)}°{truth_state.stream_unit} preview_valid_time_gmt={closest.valid_time_gmt} "
            f"delta={fmt_decimal(delta)}°{truth_state.stream_unit}"
        ),
        {
            "page_url": truth_state.page_url,
            "location_id": truth_state.location_id,
            "stream_unit": truth_state.stream_unit,
            "source_mode": truth_state.source_mode,
            "truth_value_raw": fmt_decimal(truth_state.value_raw, 6),
            "truth_valid_time_gmt": truth_state.valid_time_gmt,
            "closest_preview_value_raw": fmt_decimal(closest.value_raw, 6),
            "closest_preview_valid_time_gmt": closest.valid_time_gmt,
            "delta": fmt_decimal(delta, 6),
        },
    )


# ============================================================
# MAIN
# ============================================================
def build_page_contexts() -> List[PageContext]:
    seen: Dict[str, bool] = {}
    contexts: List[PageContext] = []
    for page_url in TARGET_PAGES:
        if page_url in seen:
            continue
        seen[page_url] = True
        station_id, country_code = derive_station_and_country(page_url)
        contexts.append(
            PageContext(
                page_url=page_url,
                station_id=station_id,
                country_code=country_code,
                location_id=derive_location_id_from_url(page_url),
                date_key=derive_history_date_from_url(page_url),
            )
        )
    return contexts


def main() -> int:
    if RUN_MODE not in {"preview_only", "truth_only", "truth_plus_preview"}:
        raise RuntimeError(f"Unsupported RUN_MODE: {RUN_MODE!r}")

    page_contexts = build_page_contexts()
    if len(page_contexts) != 3:
        raise RuntimeError(f"Expected exactly 3 unique TARGET_PAGES, got {len(page_contexts)}")

    session = build_session()
    memory = MonitorMemory()

    log("CFG", f"RUN_MODE={RUN_MODE}")
    log("CFG", f"TARGET_PAGES={TARGET_PAGES}")
    log("CFG", f"CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}")
    log("CFG", f"TRUTH_ARRAY_POINT_MODE={TRUTH_ARRAY_POINT_MODE}")
    log("CFG", f"API_MODE_ORDER={API_MODE_ORDER}")
    log("CFG", f"ENABLE_DESKTOP_ALERT={ENABLE_DESKTOP_ALERT}")
    log("CFG", f"ENABLE_BELL_ALERT={ENABLE_BELL_ALERT}")

    for ctx in page_contexts:
        log("CFG", f"PAGE={ctx.page_url} STATION={ctx.station_id} LOCATION_ID={ctx.location_id} HISTORY_DATE={ctx.date_key}")

    if LOG_TO_FILE:
        log("CFG", f"LOG_FILE_PATH={LOGGER.text_path}")
    if WRITE_JSONL_EVENTS:
        log("CFG", f"JSONL_FILE_PATH={LOGGER.jsonl_path}")

    while True:
        try:
            memory.poll_count += 1

            for ctx in page_contexts:
                if RUN_MODE in {"preview_only", "truth_plus_preview"}:
                    preview_states = fetch_preview_states(session, memory, ctx)
                    process_preview_state(memory, preview_states["C"])
                    process_preview_state(memory, preview_states["F"])

                if RUN_MODE in {"truth_only", "truth_plus_preview"}:
                    truth_states = fetch_truth_states(session, memory, ctx, TRUTH_ARRAY_POINT_MODE)
                    process_truth_state(memory, truth_states["C"])
                    process_truth_state(memory, truth_states["F"])

        except KeyboardInterrupt:
            log("STOP", "Stopped by user.")
            print()
            input("Press Enter to exit...")
            return 0

        except (RuntimeError, requests.RequestException) as exc:
            log("ERR", f"{type(exc).__name__}: {exc}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        rc = main()
        print()
        input("Press Enter to exit...")
        raise SystemExit(rc)
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[FATAL] {type(exc).__name__}: {exc}", flush=True)
        print()
        input("Press Enter to exit...")
        raise
