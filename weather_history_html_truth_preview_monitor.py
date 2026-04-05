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
    "https://www.wunderground.com/history/daily/us/tx/houston/KHOU/date/2026-4-5",
    "https://www.wunderground.com/history/daily/it/ciampino/LIRA/date/2026-4-5",
    "https://www.wunderground.com/history/daily/hu/budapest/LHBP/date/2026-4-5",
]

API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
RUN_MODE = "truth_plus_preview"  # preview_only | truth_only | truth_plus_preview
CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True

# Truth selection from historical observations array
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

# Alerts: desktop notifications only for truth changes.
ENABLE_DESKTOP_ALERT = True
ENABLE_BELL_ALERT = False
DESKTOP_ALERT_TITLE = "Weather Truth Change"
DESKTOP_ALERT_DURATION_SECONDS = 5


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
    location_id: str
    date_key: str


@dataclass
class MonitorMemory:
    poll_count: int = 0
    last_truth_identity_by_key: Dict[str, Optional[str]] = field(default_factory=dict)
    last_preview_identity_by_key: Dict[str, Optional[str]] = field(default_factory=dict)
    pending_preview_by_key: Dict[str, Dict[str, PreviewState]] = field(default_factory=dict)
    last_alerted_truth_value_by_key: Dict[str, Optional[str]] = field(default_factory=dict)


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


def derive_location_id_from_url(url: str) -> str:
    match = re.search(
        r"/history/daily/([a-z]{2})/.+?/([A-Z0-9]{3,8})/date/\d{4}-\d{1,2}-\d{1,2}(?:/|$)",
        url,
        re.IGNORECASE,
    )
    if not match:
        raise RuntimeError(f"Cannot derive station from URL: {url}")
    country = match.group(1).upper()
    station = match.group(2).upper()
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


def historical_url(location_id: str) -> str:
    return f"https://api.weather.com/v1/location/{location_id}/observations/historical.json"


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


# ============================================================
# ARRAY POINT SELECTION
# ============================================================
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
# EXTRACTION
# ============================================================
def extract_preview_state(page_ctx: PageContext, data: Dict[str, Any], units: str) -> PreviewState:
    observations = data.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError(f"Historical API returned no observations for preview units={units!r} on {page_ctx.page_url}")

    observation = observations[-1]
    if not isinstance(observation, dict) or "temp" not in observation:
        raise RuntimeError(f"Preview observation has no temp field for units={units!r} on {page_ctx.page_url}")

    metadata = data.get("metadata") or {}
    stream_unit = "F" if units.lower() == "e" else "C"

    return PreviewState(
        page_url=page_ctx.page_url,
        location_id=page_ctx.location_id,
        stream_unit=stream_unit,
        value_raw=parse_decimal(observation.get("temp"), f"preview.temp.{units}"),
        valid_time_gmt=coerce_int(observation.get("valid_time_gmt")),
        expire_time_gmt=coerce_int(observation.get("expire_time_gmt") or metadata.get("expire_time_gmt")),
        obs_name=str(observation.get("obs_name") or ""),
        obs_id=str(observation.get("obs_id") or ""),
        wx_phrase=str(observation.get("wx_phrase") or ""),
        wdir_cardinal=str(observation.get("wdir_cardinal") or "") if observation.get("wdir_cardinal") is not None else None,
        wspd=maybe_decimal(observation.get("wspd")),
        first_seen_epoch=0.0,
        first_seen_wall="",
    )


def extract_truth_state(page_ctx: PageContext, data: Dict[str, Any], units: str, point_mode: str) -> TruthState:
    observations = data.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError(f"Historical API returned no observations for truth units={units!r} on {page_ctx.page_url}")

    row_index, observation = select_observation(observations, point_mode)
    if not isinstance(observation, dict) or "temp" not in observation:
        raise RuntimeError(f"Truth observation has no temp field for units={units!r} on {page_ctx.page_url}")

    metadata = data.get("metadata") or {}
    stream_unit = "F" if units.lower() == "e" else "C"

    return TruthState(
        page_url=page_ctx.page_url,
        location_id=page_ctx.location_id,
        stream_unit=stream_unit,
        value_raw=parse_decimal(observation.get("temp"), f"truth.temp.{units}"),
        valid_time_gmt=coerce_int(observation.get("valid_time_gmt")),
        expire_time_gmt=coerce_int(observation.get("expire_time_gmt") or metadata.get("expire_time_gmt")),
        obs_name=str(observation.get("obs_name") or ""),
        obs_id=str(observation.get("obs_id") or ""),
        wx_phrase=str(observation.get("wx_phrase") or ""),
        row_index=row_index,
        row_count=len(observations),
        source_mode=f"historical_json:{point_mode}",
    )


def fetch_preview_states(session: requests.Session, page_ctx: PageContext) -> Dict[str, PreviewState]:
    data_c = fetch_historical_json(session, page_ctx.location_id, "m", page_ctx.date_key)
    data_f = fetch_historical_json(session, page_ctx.location_id, "e", page_ctx.date_key)
    return {
        "C": extract_preview_state(page_ctx, data_c, "m"),
        "F": extract_preview_state(page_ctx, data_f, "e"),
    }


def fetch_truth_states(session: requests.Session, page_ctx: PageContext, point_mode: str) -> Dict[str, TruthState]:
    data_c = fetch_historical_json(session, page_ctx.location_id, "m", page_ctx.date_key)
    data_f = fetch_historical_json(session, page_ctx.location_id, "e", page_ctx.date_key)
    return {
        "C": extract_truth_state(page_ctx, data_c, "m", point_mode),
        "F": extract_truth_state(page_ctx, data_f, "e", point_mode),
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
            f"page={state.page_url} stream={state.stream_unit} value={fmt_decimal(state.value_raw)}°{state.stream_unit} "
            f"valid_time_gmt={state.valid_time_gmt} ({epoch_to_utc_str(state.valid_time_gmt)}) "
            f"expire_time_gmt={state.expire_time_gmt} ({epoch_to_utc_str(state.expire_time_gmt)}) "
            f"obs_name={state.obs_name!r} wx={state.wx_phrase!r}"
        ),
        {
            "page_url": state.page_url,
            "location_id": state.location_id,
            "stream_unit": state.stream_unit,
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

    new_value_key = decimal_key(state.value_raw)
    if memory.last_alerted_truth_value_by_key.get(key) != new_value_key:
        emit_truth_alert(state)
        memory.last_alerted_truth_value_by_key[key] = new_value_key

    state.first_seen_epoch = time.time()
    state.first_seen_wall = LOGGER.now_str()
    memory.last_truth_identity_by_key[key] = identity

    log(
        "TRUTH_NEW",
        (
            f"page={state.page_url} stream={state.stream_unit} source_mode={state.source_mode} "
            f"value={fmt_decimal(state.value_raw)}°{state.stream_unit} row_index={state.row_index} row_count={state.row_count} "
            f"valid_time_gmt={state.valid_time_gmt} ({epoch_to_utc_str(state.valid_time_gmt)}) "
            f"obs_name={state.obs_name!r} wx={state.wx_phrase!r}"
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
        contexts.append(
            PageContext(
                page_url=page_url,
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
    log("CFG", f"TRUTH_SOURCE=historical_json")
    log("CFG", f"TRUTH_ARRAY_POINT_MODE={TRUTH_ARRAY_POINT_MODE}")
    log("CFG", f"ENABLE_DESKTOP_ALERT={ENABLE_DESKTOP_ALERT}")
    log("CFG", f"ENABLE_BELL_ALERT={ENABLE_BELL_ALERT}")

    for ctx in page_contexts:
        log("CFG", f"PAGE={ctx.page_url} LOCATION_ID={ctx.location_id} HISTORY_DATE={ctx.date_key}")

    if LOG_TO_FILE:
        log("CFG", f"LOG_FILE_PATH={LOGGER.text_path}")
    if WRITE_JSONL_EVENTS:
        log("CFG", f"JSONL_FILE_PATH={LOGGER.jsonl_path}")

    while True:
        try:
            memory.poll_count += 1

            for ctx in page_contexts:
                if RUN_MODE in {"preview_only", "truth_plus_preview"}:
                    preview_states = fetch_preview_states(session, ctx)
                    process_preview_state(memory, preview_states["C"])
                    process_preview_state(memory, preview_states["F"])

                if RUN_MODE in {"truth_only", "truth_plus_preview"}:
                    truth_states = fetch_truth_states(session, ctx, TRUTH_ARRAY_POINT_MODE)
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
