#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Truth-only Weather Underground monitor
-------------------------------------
- Monitors exactly 3 WU history pages
- Uses dense API order with graceful fallback: v3 -> v2 -> v1
- No preview logic
- Desktop alerts via plyer only
- C and F streams are independent
- No duplicate alerts for identical temperature values
- Detects new observations inside the tail of the array
- Logs cadence from recent observations
- Double-click runnable and never auto-closes
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import requests
from plyer import notification

# ============================================================
# CONFIG
# ============================================================
TARGET_PAGES = [
    "https://www.wunderground.com/history/daily/us/tx/houston/KHOU/date/2026-4-5",
    "https://www.wunderground.com/history/daily/it/ciampino/LIRA/date/2026-4-5",
    "https://www.wunderground.com/history/daily/hu/budapest/LHBP/date/2026-4-5",
]

API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True
USER_AGENT = "Mozilla/5.0"

# latest = last row, previous = previous row, penultimate_nonempty = last row with temp
TRUTH_ARRAY_POINT_MODE = "latest"  # latest | previous | penultimate_nonempty

ENABLE_DESKTOP_ALERT = True
DESKTOP_ALERT_TITLE = "Weather Truth Change"
DESKTOP_ALERT_TIMEOUT_SECONDS = 5

# Endpoint priority. v3 is densest when authorized.
API_MODE_ORDER = ["v3", "v2", "v1"]

# Tail scanning / cadence logging
TAIL_SCAN_COUNT = 5
SEEN_IDENTITY_MAXLEN = 64
CADENCE_LOG_TAIL_COUNT = 5


# ============================================================
# DATA MODELS
# ============================================================
@dataclass
class PageContext:
    page_url: str
    station_id: str
    country_code: str
    location_id: str
    date_key: str


@dataclass
class TruthState:
    page_url: str
    station_id: str
    stream_unit: str
    value_raw: Decimal
    valid_time_gmt: int
    row_index: int
    row_count: int
    source_mode: str
    obs_name: str = ""
    wx_phrase: str = ""


@dataclass
class Memory:
    last_identity: Dict[str, Optional[str]] = field(default_factory=dict)
    last_alerted_value: Dict[str, Optional[str]] = field(default_factory=dict)
    seen_tail_identities: Dict[str, Deque[str]] = field(default_factory=dict)
    seen_tail_sets: Dict[str, Set[str]] = field(default_factory=dict)
    last_cadence_signature: Dict[str, Optional[str]] = field(default_factory=dict)
    last_accepted_valid_time: Dict[str, int] = field(default_factory=dict)
    last_accepted_row_count: Dict[str, int] = field(default_factory=dict)


# ============================================================
# LOGGING
# ============================================================
def log(tag: str, text: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{tag}] {text}", flush=True)


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


def epoch_to_str(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def epoch_to_hm(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M")


def derive_station_and_country(url: str) -> Tuple[str, str]:
    m = re.search(r"/history/daily/([a-z]{2})/.+?/([A-Z0-9]{3,8})/date/", url, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"Cannot parse station from {url}")
    return m.group(2).upper(), m.group(1).upper()


def derive_date(url: str) -> str:
    m = re.search(r"/date/(\d{4})-(\d{1,2})-(\d{1,2})", url)
    if not m:
        raise RuntimeError(f"Cannot parse date from {url}")
    return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"


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
        "obs_name": dict_get_ci(row, "obs_name", "obsName", "stationName"),
        "wx_phrase": dict_get_ci(row, "wx_phrase", "iconPhrase", "phrase", "condition"),
    }


def flatten_parallel_observations(obs_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    temp_key = None
    time_key = None

    for key, value in obs_obj.items():
        if not isinstance(value, list):
            continue
        kl = str(key).lower()
        if temp_key is None and kl in {"temperature", "temp"}:
            temp_key = key
        if time_key is None and kl in {"validtimeutc", "valid_time_gmt", "epochtimeutc", "time"}:
            time_key = key

    if temp_key is None:
        return []

    temps = obs_obj.get(temp_key)
    if not isinstance(temps, list) or not temps:
        return []

    times = obs_obj.get(time_key) if time_key else []
    rows: List[Dict[str, Any]] = []
    for i in range(len(temps)):
        row = {
            "temp": temps[i],
            "valid_time_gmt": times[i] if isinstance(times, list) and i < len(times) else None,
        }
        rows.append(normalize_row(row))
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


def select_row(rows: List[Dict[str, Any]], mode: str) -> Tuple[int, Dict[str, Any]]:
    if not rows:
        raise RuntimeError("Cannot select row from empty observations array")

    if mode == "latest":
        return len(rows) - 1, rows[-1]
    if mode == "previous":
        if len(rows) >= 2:
            return len(rows) - 2, rows[-2]
        return len(rows) - 1, rows[-1]
    if mode == "penultimate_nonempty":
        for idx in range(len(rows) - 1, -1, -1):
            if maybe_decimal(rows[idx].get("temp")) is not None:
                return idx, rows[idx]
        return len(rows) - 1, rows[-1]

    raise RuntimeError(f"Unsupported TRUTH_ARRAY_POINT_MODE: {mode!r}")


def row_identity(row: Dict[str, Any]) -> Optional[str]:
    temp = maybe_decimal(row.get("temp"))
    valid_time = int(row.get("valid_time_gmt") or 0)
    if temp is None or valid_time <= 0:
        return None
    return f"{valid_time}|{decimal_key(temp)}"


def update_seen_identity(memory: Memory, key: str, identity: str) -> bool:
    dq = memory.seen_tail_identities.setdefault(key, deque())
    st = memory.seen_tail_sets.setdefault(key, set())
    if identity in st:
        return False
    dq.append(identity)
    st.add(identity)
    while len(dq) > SEEN_IDENTITY_MAXLEN:
        old = dq.popleft()
        st.discard(old)
    return True


# ============================================================
# ALERTS
# ============================================================
def emit_alert(title: str, message: str) -> None:
    try:
        notification.notify(title=title, message=message, timeout=DESKTOP_ALERT_TIMEOUT_SECONDS)
    except Exception as exc:
        log("ALERT_ERR", f"plyer failed: {type(exc).__name__}: {exc}")


# ============================================================
# HTTP
# ============================================================
def build_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Referer"] = "https://www.wunderground.com/"
    return session


def fetch_v3(session: requests.Session, station_id: str, units: str, date_key: str) -> Dict[str, Any]:
    r = session.get(
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
    r.raise_for_status()
    return r.json()


def fetch_v2(session: requests.Session, station_id: str, units: str, date_key: str) -> Dict[str, Any]:
    r = session.get(
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
    r.raise_for_status()
    return r.json()


def fetch_v1(session: requests.Session, location_id: str, units: str, date_key: str) -> Dict[str, Any]:
    r = session.get(
        f"https://api.weather.com/v1/location/{location_id}/observations/historical.json",
        params={
            "startDate": date_key,
            "endDate": date_key,
            "units": units,
            "apiKey": API_KEY,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
    r.raise_for_status()
    return r.json()


def fetch_dense_rows(session: requests.Session, page: PageContext, units: str) -> Tuple[List[Dict[str, Any]], str]:
    last_error = ""

    for mode in API_MODE_ORDER:
        try:
            if mode == "v3":
                payload = fetch_v3(session, page.station_id, units, page.date_key)
            elif mode == "v2":
                payload = fetch_v2(session, page.station_id, units, page.date_key)
            elif mode == "v1":
                payload = fetch_v1(session, page.location_id, units, page.date_key)
            else:
                continue

            rows = extract_rows(payload)
            if rows:
                return rows, mode
            last_error = f"{mode}: no usable rows"
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if mode == "v3" and status in {401, 403}:
                last_error = f"v3 unauthorized ({status})"
                continue
            last_error = f"{mode}: HTTP {status}: {exc}"
            continue
        except Exception as exc:
            last_error = f"{mode}: {type(exc).__name__}: {exc}"
            continue

    raise RuntimeError(f"No usable rows for page={page.page_url} units={units}. Last error: {last_error}")


# ============================================================
# CORE
# ============================================================
def build_contexts() -> List[PageContext]:
    contexts: List[PageContext] = []
    seen: Dict[str, bool] = {}
    for url in TARGET_PAGES:
        if url in seen:
            continue
        seen[url] = True
        station, country = derive_station_and_country(url)
        contexts.append(
            PageContext(
                page_url=url,
                station_id=station,
                country_code=country,
                location_id=f"{station}:9:{country}",
                date_key=derive_date(url),
            )
        )
    return contexts


def make_truth_state(page: PageContext, unit: str, rows: List[Dict[str, Any]], api_mode: str) -> TruthState:
    row_index, row = select_row(rows, TRUTH_ARRAY_POINT_MODE)
    temp = maybe_decimal(row.get("temp"))
    if temp is None:
        raise RuntimeError(f"No temp in selected row for {page.page_url} {unit}")
    valid_time = int(row.get("valid_time_gmt") or 0)
    return TruthState(
        page_url=page.page_url,
        station_id=page.station_id,
        stream_unit=unit,
        value_raw=temp,
        valid_time_gmt=valid_time,
        row_index=row_index,
        row_count=len(rows),
        source_mode=f"{api_mode}:{TRUTH_ARRAY_POINT_MODE}",
        obs_name=str(row.get("obs_name") or page.station_id),
        wx_phrase=str(row.get("wx_phrase") or ""),
    )


def process_new_observations(memory: Memory, page: PageContext, unit: str, rows: List[Dict[str, Any]], api_mode: str) -> None:
    key = f"{page.page_url}|{unit}"
    tail = rows[-TAIL_SCAN_COUNT:] if len(rows) > TAIL_SCAN_COUNT else rows[:]
    start_index = len(rows) - len(tail)

    for offset, row in enumerate(tail):
        identity = row_identity(row)
        if identity is None:
            continue
        is_new = update_seen_identity(memory, key, identity)
        if not is_new:
            continue

        temp = maybe_decimal(row.get("temp"))
        valid_time = int(row.get("valid_time_gmt") or 0)
        if temp is None or valid_time <= 0:
            continue

        log(
            "OBS_NEW",
            f"page={page.page_url} stream={unit} value={fmt_decimal(temp)}°{unit} row_index={start_index + offset} row_count={len(rows)} valid_time_gmt={valid_time} ({epoch_to_str(valid_time)}) source_mode={api_mode}:tail",
        )


def process_cadence(memory: Memory, page: PageContext, unit: str, rows: List[Dict[str, Any]], api_mode: str) -> None:
    key = f"{page.page_url}|{unit}|cadence"
    usable = [r for r in rows if int(r.get("valid_time_gmt") or 0) > 0]
    tail = usable[-CADENCE_LOG_TAIL_COUNT:] if len(usable) > CADENCE_LOG_TAIL_COUNT else usable[:]
    if len(tail) < 2:
        return

    times = [int(r.get("valid_time_gmt") or 0) for r in tail]
    deltas = [int((times[i] - times[i - 1]) / 60) for i in range(1, len(times))]
    hm = [epoch_to_hm(t) for t in times]
    signature = f"{api_mode}|{','.join(map(str, hm))}|{','.join(map(str, deltas))}"

    if memory.last_cadence_signature.get(key) == signature:
        return
    memory.last_cadence_signature[key] = signature

    log(
        "CADENCE",
        f"page={page.page_url} stream={unit} source_mode={api_mode} tail_times={hm} tail_deltas_min={deltas}",
    )


def process_state(memory: Memory, state: TruthState) -> None:
    key = f"{state.page_url}|{state.stream_unit}"
    identity = f"{state.valid_time_gmt}|{decimal_key(state.value_raw)}"
    if memory.last_identity.get(key) == identity:
        return

    last_valid_time = memory.last_accepted_valid_time.get(key)
    last_row_count = memory.last_accepted_row_count.get(key)

    if last_valid_time is not None and state.valid_time_gmt < last_valid_time:
        log(
            "ROLLBACK_SKIP",
            f"page={state.page_url} stream={state.stream_unit} candidate_time={state.valid_time_gmt} ({epoch_to_str(state.valid_time_gmt)}) candidate_row_count={state.row_count} last_time={last_valid_time} ({epoch_to_str(last_valid_time)}) last_row_count={last_row_count} source_mode={state.source_mode}",
        )
        return

    if last_valid_time is not None and last_row_count is not None:
        if state.valid_time_gmt == last_valid_time and state.row_count < last_row_count:
            log(
                "ROLLBACK_SKIP",
                f"page={state.page_url} stream={state.stream_unit} candidate_time={state.valid_time_gmt} ({epoch_to_str(state.valid_time_gmt)}) candidate_row_count={state.row_count} last_time={last_valid_time} ({epoch_to_str(last_valid_time)}) last_row_count={last_row_count} source_mode={state.source_mode}",
            )
            return

    new_value_key = decimal_key(state.value_raw)
    if memory.last_alerted_value.get(key) != new_value_key:
        emit_alert(
            DESKTOP_ALERT_TITLE,
            f"{state.obs_name or state.station_id} | {state.stream_unit} {fmt_decimal(state.value_raw)}°{state.stream_unit} | {epoch_to_str(state.valid_time_gmt)}",
        )
        memory.last_alerted_value[key] = new_value_key

    memory.last_identity[key] = identity
    memory.last_accepted_valid_time[key] = state.valid_time_gmt
    memory.last_accepted_row_count[key] = state.row_count
    log(
        "TRUTH_NEW",
        f"page={state.page_url} stream={state.stream_unit} value={fmt_decimal(state.value_raw)}°{state.stream_unit} row_index={state.row_index} row_count={state.row_count} valid_time_gmt={state.valid_time_gmt} ({epoch_to_str(state.valid_time_gmt)}) source_mode={state.source_mode}",
    )


# ============================================================
# MAIN LOOP
# ============================================================
def main() -> None:
    session = build_session()
    pages = build_contexts()
    if len(pages) != 3:
        raise RuntimeError(f"Expected exactly 3 unique TARGET_PAGES, got {len(pages)}")

    memory = Memory()

    log("CFG", f"Monitoring {len(pages)} pages")
    log("CFG", f"API_MODE_ORDER={API_MODE_ORDER}")
    log("CFG", f"TRUTH_ARRAY_POINT_MODE={TRUTH_ARRAY_POINT_MODE}")
    log("CFG", f"TAIL_SCAN_COUNT={TAIL_SCAN_COUNT}")
    log("CFG", f"CADENCE_LOG_TAIL_COUNT={CADENCE_LOG_TAIL_COUNT}")
    for page in pages:
        log("CFG", f"PAGE={page.page_url} STATION={page.station_id} LOCATION_ID={page.location_id} DATE={page.date_key}")

    while True:
        try:
            for page in pages:
                rows_c, mode_c = fetch_dense_rows(session, page, "m")
                process_new_observations(memory, page, "C", rows_c, mode_c)
                process_cadence(memory, page, "C", rows_c, mode_c)
                process_state(memory, make_truth_state(page, "C", rows_c, mode_c))

                rows_f, mode_f = fetch_dense_rows(session, page, "e")
                process_new_observations(memory, page, "F", rows_f, mode_f)
                process_cadence(memory, page, "F", rows_f, mode_f)
                process_state(memory, make_truth_state(page, "F", rows_f, mode_f))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log("ERR", f"{type(exc).__name__}: {exc}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("STOP", "Stopped by user")
        print()
        input("Press Enter to exit...")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[FATAL] {type(exc).__name__}: {exc}", flush=True)
        print()
        input("Press Enter to exit...")
        raise
