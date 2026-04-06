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
from typing import Any, Dict, List, Optional, Tuple

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
TARGET_PAGE_URL = "https://www.wunderground.com/history/daily/de/oberding/EDDM/date/2026-4-6"
LOCATION_ID = None
API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

# Modes:
# - truth_only
# - truth_plus_preview
RUN_MODE = "truth_plus_preview"

CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True

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


# ============================================================
# DATA MODELS
# ============================================================
@dataclass
class PreviewState:
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


@dataclass
class TruthState:
    stream_unit: str
    time_text: str
    value_raw: Decimal
    row_index: int
    row_count: int
    condition_text: str
    first_seen_epoch: float = 0.0
    first_seen_wall: str = ""


@dataclass
class MonitorMemory:
    poll_count: int = 0
    last_truth_identity_by_stream: Dict[str, Optional[str]] = field(default_factory=lambda: {"C": None, "F": None})
    last_preview_identity_by_stream: Dict[str, Optional[str]] = field(default_factory=lambda: {"C": None, "F": None})
    pending_preview_by_stream: Dict[str, Dict[str, PreviewState]] = field(default_factory=lambda: {"C": {}, "F": {}})


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


def decimal_key(value: Decimal) -> str:
    return fmt_decimal(value, 6)


def stream_tolerance(stream_unit: str) -> Decimal:
    return MATCH_TOLERANCE_C if stream_unit == "C" else MATCH_TOLERANCE_F


def current_date_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def derive_history_date_from_url(url: str) -> str:
    match = re.search(r"/date/(\d{4})-(\d{1,2})-(\d{1,2})(?:/|$)", url, re.IGNORECASE)
    if not match:
        return current_date_yyyymmdd()
    return f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"


def derive_location_id_from_url(url: str) -> str:
    match = re.search(r"/history/daily/([a-z]{2})/.+?/([A-Z0-9]{3,8})(?:/|$)", url, re.IGNORECASE)
    if not match:
        raise RuntimeError(f"Cannot derive station from URL: {url}")
    country = match.group(1).upper()
    station = match.group(2).upper()
    return f"{station}:9:{country}"


def epoch_to_utc_str(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def strip_html_tags(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", value)
    no_entities = (
        no_tags.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#176;", "°")
    )
    return re.sub(r"\s+", " ", no_entities).strip()


def truth_identity(state: TruthState) -> str:
    return f"{state.stream_unit}|{state.time_text}|{decimal_key(state.value_raw)}|{state.condition_text}"


def preview_identity(state: PreviewState) -> str:
    return f"{state.stream_unit}|{state.valid_time_gmt}|{decimal_key(state.value_raw)}"


def preview_bucket_key(state: PreviewState) -> str:
    return f"{decimal_key(state.value_raw)}|{state.valid_time_gmt}"


def parse_temperature_text(text: str) -> Tuple[Decimal, str]:
    normalized = str(text or "").replace("\xa0", " ").strip()
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°\s*([CF])", normalized, re.IGNORECASE)
    if not match:
        raise RuntimeError(f"Could not parse temperature text: {text!r}")
    value = parse_decimal(match.group(1), "temperature_text")
    unit = match.group(2).upper()
    return value, unit


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


def historical_url(location_id: str) -> str:
    return f"https://api.weather.com/v1/location/{location_id}/observations/historical.json"


def resolve_location_id_from_page_api(session: requests.Session, page_url: str) -> str:
    response = session.get(page_url, timeout=REQUEST_TIMEOUT_SECONDS, verify=VERIFY_TLS)
    response.raise_for_status()
    html = response.text

    patterns = [
        r"/v1/location/([^/]+)/observations/historical\.json",
        r"https://api\.weather\.com/v1/location/([^/]+)/observations/historical\.json",
        r'"locationKey"\s*:\s*"([^"]+)"',
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = match.group(1)
            if ":9:" in value:
                return value

    return derive_location_id_from_url(page_url)


def fetch_page_html(session: requests.Session, page_url: str) -> str:
    response = session.get(page_url, timeout=REQUEST_TIMEOUT_SECONDS, verify=VERIFY_TLS)
    response.raise_for_status()
    return response.text


def fetch_historical_json(session: requests.Session, location_id: str, units: str, date_key: str) -> Dict[str, Any]:
    response = session.get(
        historical_url(location_id),
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


def extract_latest_preview_state(data: Dict[str, Any], units: str) -> PreviewState:
    observations = data.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError(f"Historical API returned no observations for units={units!r}.")

    observation = observations[-1]
    if not isinstance(observation, dict) or "temp" not in observation:
        raise RuntimeError(f"Last observation has no temp field for units={units!r}.")

    metadata = data.get("metadata") or {}
    stream_unit = "F" if units.lower() == "e" else "C"

    return PreviewState(
        stream_unit=stream_unit,
        value_raw=parse_decimal(observation.get("temp"), f"preview.temp.{units}"),
        valid_time_gmt=int(observation.get("valid_time_gmt") or 0),
        expire_time_gmt=int(observation.get("expire_time_gmt") or metadata.get("expire_time_gmt") or 0),
        obs_name=str(observation.get("obs_name") or ""),
        obs_id=str(observation.get("obs_id") or ""),
        wx_phrase=str(observation.get("wx_phrase") or ""),
        wdir_cardinal=str(observation.get("wdir_cardinal") or "") if observation.get("wdir_cardinal") is not None else None,
        wspd=parse_decimal(observation.get("wspd"), f"preview.wspd.{units}") if observation.get("wspd") is not None else None,
        first_seen_epoch=0.0,
        first_seen_wall="",
    )


def fetch_preview_states(session: requests.Session, location_id: str, date_key: str) -> Dict[str, PreviewState]:
    states: Dict[str, PreviewState] = {}

    data_c = fetch_historical_json(session, location_id, "m", date_key)
    states["C"] = extract_latest_preview_state(data_c, "m")

    data_f = fetch_historical_json(session, location_id, "e", date_key)
    states["F"] = extract_latest_preview_state(data_f, "e")

    return states


# ============================================================
# HTML TRUTH PARSER
# ============================================================
def parse_truth_states_from_html(html: str) -> Dict[str, TruthState]:
    table_match = re.search(
        r'<lib-city-history-observation\b.*?<table\b[^>]*class="[^"]*\bmat-mdc-table\b[^"]*"[^>]*>(.*?)</table>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not table_match:
        raise RuntimeError("Daily Observations mat-table not found in HTML.")

    table_html = table_match.group(1)

    row_matches = re.findall(
        r'<tr\b[^>]*class="[^"]*\bmat-mdc-row\b[^"]*"[^>]*>(.*?)</tr>',
        table_html,
        re.IGNORECASE | re.DOTALL,
    )
    if not row_matches:
        raise RuntimeError("No data rows found in Daily Observations table HTML.")

    last_row_html = row_matches[-1]

    def extract_cell(row_html: str, column_name: str) -> str:
        pattern = (
            r'<td\b[^>]*class="[^"]*\bmat-column-' + re.escape(column_name) + r'\b[^"]*"[^>]*>(.*?)</td>'
        )
        match = re.search(pattern, row_html, re.IGNORECASE | re.DOTALL)
        if not match:
            return ""
        return strip_html_tags(match.group(1))

    time_text = extract_cell(last_row_html, "dateString")
    temp_text = extract_cell(last_row_html, "temperature")
    condition_text = extract_cell(last_row_html, "condition")

    if not time_text:
        raise RuntimeError("Could not extract dateString from last observation row.")
    if not temp_text:
        raise RuntimeError("Could not extract temperature from last observation row.")

    value_raw, stream_unit = parse_temperature_text(temp_text)

    state = TruthState(
        stream_unit=stream_unit,
        time_text=time_text,
        value_raw=value_raw,
        row_index=len(row_matches) - 1,
        row_count=len(row_matches),
        condition_text=condition_text,
    )

    return {stream_unit: state}


def collect_html_debug_snapshot(html: str) -> Dict[str, Any]:
    try:
        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = strip_html_tags(title_match.group(1)) if title_match else ""

        header_matches = re.findall(
            r'<th\b[^>]*class="[^"]*\bmat-column-([a-zA-Z0-9_]+)\b[^"]*"[^>]*>.*?<div class="mat-sort-header-content">(.*?)</div>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        headers = [{"column": col, "text": strip_html_tags(txt)} for col, txt in header_matches]

        row_matches = re.findall(
            r'<tr\b[^>]*class="[^"]*\bmat-mdc-row\b[^"]*"[^>]*>(.*?)</tr>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        last_row_text = strip_html_tags(row_matches[-1]) if row_matches else ""

        legends = re.findall(
            r'<div class="legend-def[^"]*">\s*<span class="legend-key"></span><span>(.*?)</span>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        legends = [strip_html_tags(x) for x in legends]

        return {
            "title": title,
            "rowCount": len(row_matches),
            "lastRowText": last_row_text,
            "headerTexts": headers,
            "chartLegends": legends,
        }
    except Exception as exc:
        return {"error": str(exc)}


# ============================================================
# MATCHER
# ============================================================
def process_preview_state(memory: MonitorMemory, state: PreviewState) -> None:
    identity = preview_identity(state)
    if identity == memory.last_preview_identity_by_stream.get(state.stream_unit):
        return

    state.first_seen_epoch = time.time()
    state.first_seen_wall = LOGGER.now_str()
    memory.last_preview_identity_by_stream[state.stream_unit] = identity
    memory.pending_preview_by_stream[state.stream_unit][preview_bucket_key(state)] = state

    log(
        "PREVIEW_NEW",
        (
            f"stream={state.stream_unit} value={fmt_decimal(state.value_raw)}°{state.stream_unit} "
            f"valid_time_gmt={state.valid_time_gmt} ({epoch_to_utc_str(state.valid_time_gmt) if state.valid_time_gmt else 'n/a'}) "
            f"expire_time_gmt={state.expire_time_gmt} ({epoch_to_utc_str(state.expire_time_gmt) if state.expire_time_gmt else 'n/a'}) "
            f"obs_name={state.obs_name!r} wx={state.wx_phrase!r}"
        ),
        {
            "stream_unit": state.stream_unit,
            "value_raw": fmt_decimal(state.value_raw, 6),
            "valid_time_gmt": state.valid_time_gmt,
            "expire_time_gmt": state.expire_time_gmt,
            "obs_name": state.obs_name,
            "obs_id": state.obs_id,
            "wx_phrase": state.wx_phrase,
            "wdir_cardinal": state.wdir_cardinal,
            "wspd": fmt_decimal(state.wspd, 6) if state.wspd is not None else None,
            "preview_identity": identity,
        },
    )


def process_truth_state(memory: MonitorMemory, state: TruthState) -> None:
    identity = truth_identity(state)
    if identity == memory.last_truth_identity_by_stream.get(state.stream_unit):
        return

    state.first_seen_epoch = time.time()
    state.first_seen_wall = LOGGER.now_str()
    memory.last_truth_identity_by_stream[state.stream_unit] = identity

    log(
        "TRUTH_NEW",
        (
            f"stream={state.stream_unit} time={state.time_text!r} "
            f"value={fmt_decimal(state.value_raw)}°{state.stream_unit} "
            f"row_index={state.row_index} row_count={state.row_count} "
            f"condition={state.condition_text!r}"
        ),
        {
            "stream_unit": state.stream_unit,
            "time_text": state.time_text,
            "value_raw": fmt_decimal(state.value_raw, 6),
            "row_index": state.row_index,
            "row_count": state.row_count,
            "condition_text": state.condition_text,
            "truth_identity": identity,
        },
    )

    evaluate_matches(memory, state)


def evaluate_matches(memory: MonitorMemory, truth_state: TruthState) -> None:
    stream_unit = truth_state.stream_unit
    pending_map = memory.pending_preview_by_stream[stream_unit]
    if not pending_map:
        return

    candidates = list(pending_map.values())
    if not candidates:
        return

    same_value_candidates = [
        item
        for item in candidates
        if abs(item.value_raw - truth_state.value_raw) <= stream_tolerance(stream_unit)
    ]

    if same_value_candidates:
        match = max(same_value_candidates, key=lambda x: x.valid_time_gmt)
        lag_seconds = max(0.0, truth_state.first_seen_epoch - match.first_seen_epoch)

        log(
            "MATCH",
            (
                f"stream={stream_unit} truth_value={fmt_decimal(truth_state.value_raw)}°{stream_unit} "
                f"truth_time={truth_state.time_text!r} "
                f"preview_valid_time_gmt={match.valid_time_gmt} "
                f"lag_seconds={lag_seconds:.1f}"
            ),
            {
                "stream_unit": stream_unit,
                "truth_value_raw": fmt_decimal(truth_state.value_raw, 6),
                "truth_time_text": truth_state.time_text,
                "preview_valid_time_gmt": match.valid_time_gmt,
                "preview_value_raw": fmt_decimal(match.value_raw, 6),
                "lag_seconds": round(lag_seconds, 3),
                "preview_first_seen_wall": match.first_seen_wall,
                "truth_first_seen_wall": truth_state.first_seen_wall,
            },
        )

        to_delete: List[str] = []
        for key, item in pending_map.items():
            if (
                abs(item.value_raw - truth_state.value_raw) <= stream_tolerance(stream_unit)
                and item.valid_time_gmt <= match.valid_time_gmt
            ):
                to_delete.append(key)
        for key in to_delete:
            pending_map.pop(key, None)
        return

    closest = min(candidates, key=lambda x: abs(x.value_raw - truth_state.value_raw))
    delta = abs(closest.value_raw - truth_state.value_raw)

    log(
        "DIFF",
        (
            f"stream={stream_unit} truth_value={fmt_decimal(truth_state.value_raw)}°{stream_unit} "
            f"truth_time={truth_state.time_text!r} "
            f"closest_preview_value={fmt_decimal(closest.value_raw)}°{stream_unit} "
            f"preview_valid_time_gmt={closest.valid_time_gmt} "
            f"delta={fmt_decimal(delta)}°{stream_unit}"
        ),
        {
            "stream_unit": stream_unit,
            "truth_value_raw": fmt_decimal(truth_state.value_raw, 6),
            "truth_time_text": truth_state.time_text,
            "closest_preview_value_raw": fmt_decimal(closest.value_raw, 6),
            "closest_preview_valid_time_gmt": closest.valid_time_gmt,
            "delta": fmt_decimal(delta, 6),
        },
    )


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    global LOCATION_ID

    if RUN_MODE not in {"truth_only", "truth_plus_preview"}:
        raise RuntimeError(f"Unsupported RUN_MODE: {RUN_MODE!r}")

    session = build_session()
    date_key = derive_history_date_from_url(TARGET_PAGE_URL)

    if LOCATION_ID is None:
        LOCATION_ID = resolve_location_id_from_page_api(session, TARGET_PAGE_URL)

    memory = MonitorMemory()

    log("CFG", f"RUN_MODE={RUN_MODE}")
    log("CFG", f"TARGET_PAGE_URL={TARGET_PAGE_URL}")
    log("CFG", f"LOCATION_ID={LOCATION_ID}")
    log("CFG", f"HISTORY_DATE={date_key}")
    log("CFG", f"CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}")

    if LOG_TO_FILE:
        log("CFG", f"LOG_FILE_PATH={LOGGER.text_path}")

    if WRITE_JSONL_EVENTS:
        log("CFG", f"JSONL_FILE_PATH={LOGGER.jsonl_path}")

    while True:
        try:
            memory.poll_count += 1

            if RUN_MODE == "truth_plus_preview":
                preview_states = fetch_preview_states(session, LOCATION_ID, date_key)
                for stream_unit in ("C", "F"):
                    process_preview_state(memory, preview_states[stream_unit])

            html = fetch_page_html(session, TARGET_PAGE_URL)
            truth_states = parse_truth_states_from_html(html)

            for stream_unit in ("C", "F"):
                state = truth_states.get(stream_unit)
                if state is not None:
                    process_truth_state(memory, state)
                else:
                    log(
                        "TRUTH_MISSING",
                        f"stream={stream_unit} truth HTML table does not currently expose this unit on page",
                        collect_html_debug_snapshot(html),
                    )

        except KeyboardInterrupt:
            log("STOP", "Stopped by user.")
            print()
            input("Press Enter to exit...")
            return 0

        except (RuntimeError, requests.RequestException) as exc:
            dbg_html = ""
            try:
                dbg_html = fetch_page_html(session, TARGET_PAGE_URL)
                dbg = collect_html_debug_snapshot(dbg_html)
            except Exception:
                dbg = {}
            log("ERR", f"{type(exc).__name__}: {exc} | debug={dbg}")

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
        print(f"[FATAL] {type(exc).__name__}: {exc}", flush=True)
        print()
        input("Press Enter to exit...")
        raise