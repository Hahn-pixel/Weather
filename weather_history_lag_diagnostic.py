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

try:
    from selenium import webdriver
    from selenium.common.exceptions import InvalidSessionIdException, TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except Exception as exc:
    print(f"[FATAL] Failed to import selenium: {exc}", flush=True)
    print("[HINT] Run: py -m pip install selenium", flush=True)
    print()
    input("Press Enter to exit...")
    raise


# =========================
# CONFIG
# =========================
TARGET_PAGE_URL = "https://www.wunderground.com/history/daily/fr/paris/LFPI/date/2026-4-3"
LOCATION_ID = None  # auto-detected from TARGET_PAGE_URL
API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
API_UNITS = "m"  # 'm' = Celsius, 'e' = Fahrenheit
CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True
SHOW_BROWSER_WINDOW = False
UI_RENDER_MODE = "AUTO"  # "AUTO", "C", or "F"
PAGE_LOAD_TIMEOUT_SECONDS = 40
SVG_WAIT_TIMEOUT_SECONDS = 20
REBUILD_BROWSER_EVERY_N_POLLS = 120
LOG_TO_FILE = True
LOG_FILE_PATH = "weather_history_lag_diagnostic.log"
WRITE_JSONL_EVENTS = True
JSONL_FILE_PATH = "weather_history_lag_diagnostic.jsonl"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
REFERER = "https://www.wunderground.com/"


# =========================
# URL → LOCATION_ID RESOLUTION
# =========================

def derive_location_id_from_url(url: str) -> str:
    m = re.search(r"/history/daily/([a-z]{2})/.+?/([A-Z0-9]{3,5})(?:/|$)", url, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"Cannot derive station from URL: {url}")
    country = m.group(1).upper()
    station = m.group(2).upper()
    return f"{station}:9:{country}"


def derive_history_date_from_url(url: str) -> str:
    m = re.search(r"/date/(\d{4})-(\d{1,2})-(\d{1,2})(?:/|$)", url, re.IGNORECASE)
    if not m:
        return current_date_yyyymmdd()
    year = int(m.group(1))
    month = int(m.group(2))
    day = int(m.group(3))
    return f"{year:04d}{month:02d}{day:02d}"


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
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)

    return derive_location_id_from_url(page_url)


# =========================
# DATA MODELS
# =========================
@dataclass
class ApiState:
    raw_value: Decimal
    raw_unit: str
    value_c: Decimal
    value_f: Decimal
    obs_time_utc: str
    obs_time_local: str
    valid_time_gmt: int
    observation_count: int


@dataclass
class UiState:
    value_raw: Decimal
    value_unit: str
    render_mode: str
    value_c: Decimal
    value_f: Decimal
    source: str
    path_last_y: Decimal
    path_last_x: Decimal
    y_axis_top_y: Decimal
    y_axis_bottom_y: Decimal
    y_axis_top_value: Decimal
    y_axis_bottom_value: Decimal
    path_point_count: int
    page_title: str


@dataclass
class PendingApiValue:
    value_c: Decimal
    value_f: Decimal
    first_seen_epoch: float
    first_seen_wall: str
    api_identity: str
    obs_time_local: str
    valid_time_gmt: int
    matched: bool = False


@dataclass
class MonitorMemory:
    last_api_identity: Optional[str] = None
    last_ui_identity: Optional[str] = None
    pending_by_c_key: Dict[str, PendingApiValue] = field(default_factory=dict)
    poll_count: int = 0


# =========================
# LOGGING
# =========================
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


# =========================
# HELPERS
# =========================
def parse_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Invalid decimal for {field_name}: {value!r}") from exc


def f_to_c_decimal(value_f: Decimal) -> Decimal:
    return (value_f - Decimal("32")) * Decimal("5") / Decimal("9")


def c_to_f_decimal(value_c: Decimal) -> Decimal:
    return (value_c * Decimal("9") / Decimal("5")) + Decimal("32")


def fmt_decimal(value: Decimal, places: int = 3) -> str:
    q = Decimal("1").scaleb(-places)
    return f"{value.quantize(q):f}"


def current_date_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def decimal_key_c(value_c: Decimal) -> str:
    return fmt_decimal(value_c, 6)


def epoch_to_utc_str(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# =========================
# API SIDE
# =========================
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


def fetch_historical_json(session: requests.Session) -> Dict[str, Any]:
    date_key = derive_history_date_from_url(TARGET_PAGE_URL)
    params = {
        "apiKey": API_KEY,
        "units": API_UNITS,
        "startDate": date_key,
        "endDate": date_key,
    }
    response = session.get(
        historical_url(LOCATION_ID),
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
    response.raise_for_status()
    return response.json()


def extract_api_state(data: Dict[str, Any]) -> ApiState:
    observations = data.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError("Historical API returned no observations.")

    obs = observations[-1]
    if not isinstance(obs, dict) or "temp" not in obs:
        raise RuntimeError("Last observation has no temp field.")

    raw_value = parse_decimal(obs["temp"], "api.temp")
    raw_unit = "F" if API_UNITS.lower() == "e" else "C"
    if raw_unit == "F":
        value_f = raw_value
        value_c = f_to_c_decimal(raw_value)
    else:
        value_c = raw_value
        value_f = c_to_f_decimal(raw_value)

    valid_time_gmt = int(obs.get("valid_time_gmt") or 0)
    return ApiState(
        raw_value=raw_value,
        raw_unit=raw_unit,
        value_c=value_c,
        value_f=value_f,
        obs_time_utc=str(obs.get("valid_time_gmt") or ""),
        obs_time_local=str(obs.get("obs_time_local") or obs.get("valid_time_local") or ""),
        valid_time_gmt=valid_time_gmt,
        observation_count=len(observations),
    )


def api_identity(state: ApiState) -> str:
    return f"{state.valid_time_gmt}|{fmt_decimal(state.raw_value, 6)}|{state.obs_time_local}"


# =========================
# UI SIDE (SVG)
# =========================
def build_chrome_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument(f"--user-agent={USER_AGENT}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1600,1200")
    if not SHOW_BROWSER_WINDOW:
        options.add_argument("--headless=new")
    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    return driver


def close_driver_safely(driver: Optional[webdriver.Chrome]) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


def _extract_svg_payload(driver: webdriver.Chrome) -> Dict[str, Any]:
    script = r'''
const result = {
  pageTitle: document.title || "",
  noData: false,
  pathD: null,
  yTicks: [],
};

const noDataEl = Array.from(document.querySelectorAll('*')).find(el => {
  const txt = (el.textContent || '').trim();
  return txt === 'No Data Recorded';
});
if (noDataEl) {
  result.noData = true;
}

const pathCandidates = Array.from(document.querySelectorAll('g.plot.temperature.line path, svg path'));
const lineCandidate = pathCandidates.find(el => {
  const d = (el.getAttribute('d') || '').trim();
  return d.startsWith('M') && d.includes('L');
});
if (lineCandidate) {
  result.pathD = lineCandidate.getAttribute('d');
}

const result = {
  pageTitle: document.title || "",
  noData: false,
  pathD: null,
  yTicks: [],
};

const noDataEl = Array.from(document.querySelectorAll('*')).find(el => {
  const txt = (el.textContent || '').trim();
  return txt === 'No Data Recorded';
});
if (noDataEl) {
  result.noData = true;
}

const pathCandidates = Array.from(document.querySelectorAll('g.plot.temperature.line path, svg path'));
const lineCandidate = pathCandidates.find(el => {
  const d = (el.getAttribute('d') || '').trim();
  return d.startsWith('M') && d.includes('L');
});
if (lineCandidate) {
  result.pathD = lineCandidate.getAttribute('d');
  const svg = lineCandidate.closest('svg');
  if (svg) {
    const seen = new Set();
    for (const el of Array.from(svg.querySelectorAll('text'))) {
      const text = (el.textContent || '').trim();
      const y = el.getAttribute('y');
      const x = el.getAttribute('x');
      if (!text || y === null) continue;
      const key = `${x}|${y}|${text}`;
      if (seen.has(key)) continue;
      seen.add(key);
      result.yTicks.push({x, y, text});
    }
  }
}

return result;
'''
    payload = driver.execute_script(script)
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected JS payload for SVG extraction.")
    return payload


def _parse_path_points(path_d: str) -> List[Tuple[Decimal, Decimal]]:
    tokens = re.findall(r"[ML]\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)", path_d)
    if not tokens:
        raise RuntimeError("Temperature path d=... contains no points.")
    points: List[Tuple[Decimal, Decimal]] = []
    for x_str, y_str in tokens:
        points.append((parse_decimal(x_str, "path.x"), parse_decimal(y_str, "path.y")))
    return points


def _parse_y_ticks(ticks: List[Dict[str, Any]]) -> List[Tuple[Decimal, Decimal]]:
    parsed: List[Tuple[Decimal, Decimal]] = []
    for item in ticks:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        y = item.get("y")
        if not text:
            continue
        normalized = text.replace("°", "").replace("−", "-").strip()
        if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", normalized):
            continue
        try:
            label_value = parse_decimal(normalized, "tick.text")
            label_y = parse_decimal(y, "tick.y")
        except Exception:
            continue
        parsed.append((label_y, label_value))

    parsed.sort(key=lambda pair: pair[0])

    deduped: List[Tuple[Decimal, Decimal]] = []
    seen_pairs = set()
    for item in parsed:
        key = (str(item[0]), str(item[1]))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        deduped.append(item)

    if len(deduped) < 2:
        raise RuntimeError(f"Not enough Y-axis ticks to reconstruct temperature. Parsed ticks={deduped!r}")

    best_run: List[Tuple[Decimal, Decimal]] = []
    current_run: List[Tuple[Decimal, Decimal]] = [deduped[0]]
    for item in deduped[1:]:
        prev_y, prev_value = current_run[-1]
        curr_y, curr_value = item
        if curr_y > prev_y and curr_value != prev_value:
            if len(current_run) < 2:
                current_run.append(item)
            else:
                prev_step = current_run[-1][1] - current_run[-2][1]
                curr_step = curr_value - prev_value
                if curr_step == prev_step:
                    current_run.append(item)
                else:
                    if len(current_run) > len(best_run):
                        best_run = current_run[:]
                    current_run = [current_run[-1], item]
        else:
            if len(current_run) > len(best_run):
                best_run = current_run[:]
            current_run = [item]
    if len(current_run) > len(best_run):
        best_run = current_run[:]

    if len(best_run) >= 2:
        return best_run
    return deduped


def _infer_ui_unit_from_ticks(tick_values: List[Decimal]) -> str:
    if not tick_values:
        raise RuntimeError("Cannot infer UI unit: no tick values.")
    max_abs = max(abs(v) for v in tick_values)
    return "F" if max_abs > Decimal("55") else "C"


def _resolve_ui_render_mode(inferred_unit: str) -> str:
    configured = str(UI_RENDER_MODE).strip().upper()
    if configured not in {"AUTO", "C", "F"}:
        raise RuntimeError(f"Invalid UI_RENDER_MODE: {UI_RENDER_MODE!r}")
    if configured == "AUTO":
        return inferred_unit
    return configured


def read_ui_state(driver: webdriver.Chrome) -> UiState:
    driver.get(TARGET_PAGE_URL)
    WebDriverWait(driver, SVG_WAIT_TIMEOUT_SECONDS).until(
        lambda d: d.execute_script(
            """
            const p = document.querySelector('g.plot.temperature.line path');
            return p && p.getAttribute('d') && p.getAttribute('d').length > 10;
            """
        )
    )

    payload = _extract_svg_payload(driver)
    if payload.get("noData") and not payload.get("pathD"):
        raise RuntimeError("History UI shows 'No Data Recorded'.")

    path_d = str(payload.get("pathD") or "").strip()
    if not path_d:
        raise RuntimeError("Temperature SVG path was not found.")

    points = _parse_path_points(path_d)
    ticks = _parse_y_ticks(payload.get("yTicks") or [])

    top_y, top_value = ticks[0]
    bottom_y, bottom_value = ticks[-1]
    if bottom_y == top_y:
        raise RuntimeError("Y-axis tick range is degenerate.")

    last_x, last_y = points[-1]
    ratio = (last_y - top_y) / (bottom_y - top_y)
    value_raw = top_value + (bottom_value - top_value) * ratio

    tick_values = [v for _, v in ticks]
    inferred_unit = _infer_ui_unit_from_ticks(tick_values)
    render_mode = _resolve_ui_render_mode(inferred_unit)
    value_unit = render_mode
    if value_unit == "F":
        value_f = value_raw
        value_c = f_to_c_decimal(value_raw)
    else:
        value_c = value_raw
        value_f = c_to_f_decimal(value_raw)

    return UiState(
        value_raw=value_raw,
        value_unit=value_unit,
        render_mode=render_mode,
        value_c=value_c,
        value_f=value_f,
        source="svg_temperature_line",
        path_last_y=last_y,
        path_last_x=last_x,
        y_axis_top_y=top_y,
        y_axis_bottom_y=bottom_y,
        y_axis_top_value=top_value,
        y_axis_bottom_value=bottom_value,
        path_point_count=len(points),
        page_title=str(payload.get("pageTitle") or ""),
    )


def ui_identity(state: UiState) -> str:
    return f"{state.render_mode}|{fmt_decimal(state.value_c, 6)}|{fmt_decimal(state.path_last_x, 6)}|{fmt_decimal(state.path_last_y, 6)}"


# =========================
# MATCHER
# =========================
def process_api_state(memory: MonitorMemory, state: ApiState) -> None:
    identity = api_identity(state)
    if identity == memory.last_api_identity:
        return

    memory.last_api_identity = identity
    key = decimal_key_c(state.value_c)
    now_epoch = time.time()
    existing = memory.pending_by_c_key.get(key)
    if existing is None:
        memory.pending_by_c_key[key] = PendingApiValue(
            value_c=state.value_c,
            value_f=state.value_f,
            first_seen_epoch=now_epoch,
            first_seen_wall=LOGGER.now_str(),
            api_identity=identity,
            obs_time_local=state.obs_time_local,
            valid_time_gmt=state.valid_time_gmt,
            matched=False,
        )
    else:
        existing.api_identity = identity
        existing.obs_time_local = state.obs_time_local
        existing.valid_time_gmt = state.valid_time_gmt

    log(
        "API_NEW",
        (
            f"value={fmt_decimal(state.raw_value)}°{state.raw_unit} "
            f"temp_c={fmt_decimal(state.value_c)}°C temp_f={fmt_decimal(state.value_f)}°F "
            f"valid_time_gmt={state.valid_time_gmt} ({epoch_to_utc_str(state.valid_time_gmt) if state.valid_time_gmt else 'n/a'}) "
            f"obs_time_local={state.obs_time_local} observations={state.observation_count}"
        ),
        {
            "value_c": fmt_decimal(state.value_c, 6),
            "value_f": fmt_decimal(state.value_f, 6),
            "raw_value": fmt_decimal(state.raw_value, 6),
            "raw_unit": state.raw_unit,
            "valid_time_gmt": state.valid_time_gmt,
            "obs_time_local": state.obs_time_local,
            "observation_count": state.observation_count,
            "api_identity": identity,
        },
    )


def process_ui_state(memory: MonitorMemory, state: UiState) -> None:
    identity = ui_identity(state)
    if identity == memory.last_ui_identity:
        return

    memory.last_ui_identity = identity
    log(
        "UI_NEW",
        (
            f"render_mode={state.render_mode} value={fmt_decimal(state.value_raw)}°{state.value_unit} "
            f"temp_c={fmt_decimal(state.value_c)}°C temp_f={fmt_decimal(state.value_f)}°F "
            f"source={state.source} path_last=({fmt_decimal(state.path_last_x)},{fmt_decimal(state.path_last_y)}) "
            f"y_scale_top={fmt_decimal(state.y_axis_top_value)}@{fmt_decimal(state.y_axis_top_y)} "
            f"y_scale_bottom={fmt_decimal(state.y_axis_bottom_value)}@{fmt_decimal(state.y_axis_bottom_y)} "
            f"points={state.path_point_count}"
        ),
        {
            "value_c": fmt_decimal(state.value_c, 6),
            "value_f": fmt_decimal(state.value_f, 6),
            "value_raw": fmt_decimal(state.value_raw, 6),
            "value_unit": state.value_unit,
            "render_mode": state.render_mode,
            "source": state.source,
            "path_last_x": fmt_decimal(state.path_last_x, 6),
            "path_last_y": fmt_decimal(state.path_last_y, 6),
            "y_axis_top_value": fmt_decimal(state.y_axis_top_value, 6),
            "y_axis_bottom_value": fmt_decimal(state.y_axis_bottom_value, 6),
            "path_point_count": state.path_point_count,
            "ui_identity": identity,
        },
    )
    evaluate_matches(memory, state)


def evaluate_matches(memory: MonitorMemory, ui_state: UiState) -> None:
    key = decimal_key_c(ui_state.value_c)
    pending = memory.pending_by_c_key.get(key)
    if pending is not None and not pending.matched:
        pending.matched = True
        lag_seconds = max(0.0, time.time() - pending.first_seen_epoch)
        log(
            "MATCH",
            (
                f"value_c={fmt_decimal(ui_state.value_c)}°C value_f={fmt_decimal(ui_state.value_f)}°F "
                f"lag_seconds={lag_seconds:.1f} api_first_seen={pending.first_seen_wall} "
                f"api_obs_time_local={pending.obs_time_local}"
            ),
            {
                "value_c": fmt_decimal(ui_state.value_c, 6),
                "value_f": fmt_decimal(ui_state.value_f, 6),
                "lag_seconds": round(lag_seconds, 3),
                "api_first_seen_wall": pending.first_seen_wall,
                "api_obs_time_local": pending.obs_time_local,
                "api_identity": pending.api_identity,
                "ui_identity": ui_identity(ui_state),
            },
        )
        return

    unmatched_values = [p for p in memory.pending_by_c_key.values() if not p.matched]
    if unmatched_values:
        closest = min(unmatched_values, key=lambda p: abs(p.value_c - ui_state.value_c))
        log(
            "DIFF",
            (
                f"ui_temp_c={fmt_decimal(ui_state.value_c)}°C ui_temp_f={fmt_decimal(ui_state.value_f)}°F "
                f"closest_unmatched_api_c={fmt_decimal(closest.value_c)}°C "
                f"delta_c={fmt_decimal(abs(closest.value_c - ui_state.value_c))}°C state=WAITING_UI_OR_MISMATCH"
            ),
            {
                "ui_value_c": fmt_decimal(ui_state.value_c, 6),
                "ui_value_f": fmt_decimal(ui_state.value_f, 6),
                "closest_unmatched_api_value_c": fmt_decimal(closest.value_c, 6),
                "delta_c": fmt_decimal(abs(closest.value_c - ui_state.value_c), 6),
            },
        )


def sweep_unmatched_api(memory: MonitorMemory, newest_api_value_c: Decimal) -> None:
    to_remove: List[str] = []
    for key, pending in memory.pending_by_c_key.items():
        if pending.matched:
            continue
        if pending.value_c != newest_api_value_c:
            log(
                "API_REPLACED_BEFORE_UI",
                (
                    f"pending_value_c={fmt_decimal(pending.value_c)}°C pending_value_f={fmt_decimal(pending.value_f)}°F "
                    f"api_obs_time_local={pending.obs_time_local} replaced_by_newer_api_before_ui_match=1"
                ),
                {
                    "pending_value_c": fmt_decimal(pending.value_c, 6),
                    "pending_value_f": fmt_decimal(pending.value_f, 6),
                    "api_obs_time_local": pending.obs_time_local,
                    "api_identity": pending.api_identity,
                },
            )
            to_remove.append(key)
    for key in to_remove:
        memory.pending_by_c_key.pop(key, None)


# =========================
# MAIN
# =========================
def main() -> int:
    global LOCATION_ID

    session = build_session()

    if LOCATION_ID is None:
        LOCATION_ID = resolve_location_id_from_page_api(session, TARGET_PAGE_URL)

    driver: Optional[webdriver.Chrome] = None
    memory = MonitorMemory()

    log("CFG", f"TARGET_PAGE_URL={TARGET_PAGE_URL}")
    log("CFG", f"LOCATION_ID={LOCATION_ID} (resolved from page API)")
    log("CFG", f"API_UNITS={API_UNITS}")
    log("CFG", f"HISTORY_DATE={derive_history_date_from_url(TARGET_PAGE_URL)}")
    log("CFG", f"CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}")
    log("CFG", f"SHOW_BROWSER_WINDOW={SHOW_BROWSER_WINDOW}")
    log("CFG", f"REBUILD_BROWSER_EVERY_N_POLLS={REBUILD_BROWSER_EVERY_N_POLLS}")
    log("CFG", f"UI_RENDER_MODE={UI_RENDER_MODE}")
    if LOG_TO_FILE:
        log("CFG", f"LOG_FILE_PATH={LOGGER.text_path}")
    if WRITE_JSONL_EVENTS:
        log("CFG", f"JSONL_FILE_PATH={LOGGER.jsonl_path}")

    try:
        while True:
            try:
                memory.poll_count += 1

                api_json = fetch_historical_json(session)
                api_state = extract_api_state(api_json)
                process_api_state(memory, api_state)
                sweep_unmatched_api(memory, api_state.value_c)

                if driver is None:
                    log("BROWSER", "Creating Chrome driver...")
                    driver = build_chrome_driver()

                ui_state = read_ui_state(driver)
                process_ui_state(memory, ui_state)

                if REBUILD_BROWSER_EVERY_N_POLLS > 0 and memory.poll_count % REBUILD_BROWSER_EVERY_N_POLLS == 0:
                    log("BROWSER", f"Rebuilding browser after {REBUILD_BROWSER_EVERY_N_POLLS} polls.")
                    close_driver_safely(driver)
                    driver = None

            except KeyboardInterrupt:
                log("STOP", "Stopped by user.")
                print()
                input("Press Enter to exit...")
                return 0
            except (InvalidSessionIdException, WebDriverException) as exc:
                log("BROWSER_RESET", f"{type(exc).__name__}: browser session lost; recreating driver")
                close_driver_safely(driver)
                driver = None
            except (RuntimeError, TimeoutException, requests.RequestException) as exc:
                if isinstance(exc, RuntimeError) and "Not enough Y-axis ticks" in str(exc):
                    try:
                        dbg = driver.execute_script(
                            "return {title: document.title || '', tickTexts: Array.from(document.querySelectorAll('svg text')).map(x => (x.textContent || '').trim()).filter(Boolean).slice(0, 80), hasPath: !!document.querySelector('g.plot.temperature.line path, svg path')};"
                        ) if driver is not None else {}
                    except Exception:
                        dbg = {}
                    log("ERR", f"{type(exc).__name__}: {exc} | debug={dbg}")
                else:
                    log("ERR", f"{type(exc).__name__}: {exc}")

            time.sleep(CHECK_INTERVAL_SECONDS)
    finally:
        close_driver_safely(driver)


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
