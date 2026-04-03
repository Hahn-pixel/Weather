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
    from selenium.common.exceptions import (
        InvalidSessionIdException,
        JavascriptException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
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
LOCATION_ID = None
API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
API_UNITS = "m"  # 'm' = Celsius API, 'e' = Fahrenheit API
CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True
SHOW_BROWSER_WINDOW = False
PAGE_LOAD_TIMEOUT_SECONDS = 40
SVG_WAIT_TIMEOUT_SECONDS = 25
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
MATCH_TOLERANCE_C = Decimal("0.001")
MATCH_TOLERANCE_F = Decimal("0.001")


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
    stream_unit: str
    unit_detection_source: str
    value_raw: Decimal
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
    candidate_index: int


@dataclass
class PendingApiValue:
    stream_unit: str
    stream_value: Decimal
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
    last_ui_identity_by_stream: Dict[str, Optional[str]] = field(default_factory=lambda: {"C": None, "F": None})
    pending_by_stream_key: Dict[str, Dict[str, PendingApiValue]] = field(default_factory=lambda: {"C": {}, "F": {}})
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



def decimal_key(value: Decimal) -> str:
    return fmt_decimal(value, 6)



def current_date_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")



def derive_history_date_from_url(url: str) -> str:
    m = re.search(r"/date/(\d{4})-(\d{1,2})-(\d{1,2})(?:/|$)", url, re.IGNORECASE)
    if not m:
        return current_date_yyyymmdd()
    return f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}"



def derive_location_id_from_url(url: str) -> str:
    m = re.search(r"/history/daily/([a-z]{2})/.+?/([A-Z0-9]{3,5})(?:/|$)", url, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"Cannot derive station from URL: {url}")
    country = m.group(1).upper()
    station = m.group(2).upper()
    return f"{station}:9:{country}"



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
    response = session.get(
        historical_url(LOCATION_ID),
        params={
            "apiKey": API_KEY,
            "units": API_UNITS,
            "startDate": date_key,
            "endDate": date_key,
        },
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
        value_f = c_to_f_decimal(value_c)

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
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-features=PaintHolding")
    if not SHOW_BROWSER_WINDOW:
        options.add_argument("--headless=new")
    driver = webdriver.Chrome(service=Service(), options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
    return driver



def close_driver_safely(driver: Optional[webdriver.Chrome]) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass



def wait_for_stable_temp_svg(driver: webdriver.Chrome) -> None:
    WebDriverWait(driver, SVG_WAIT_TIMEOUT_SECONDS).until(
        lambda d: d.execute_script(
            "const ps=document.querySelectorAll('g.plot.temperature.line path'); return Array.from(ps).some(p => (p.getAttribute('d') || '').length > 10);"
        )
    )
    d1 = driver.execute_script(
        "return Array.from(document.querySelectorAll('g.plot.temperature.line path')).map(p => p.getAttribute('d') || '');"
    )
    time.sleep(0.35)
    d2 = driver.execute_script(
        "return Array.from(document.querySelectorAll('g.plot.temperature.line path')).map(p => p.getAttribute('d') || '');"
    )
    if not d1 or not d2:
        raise RuntimeError("Temperature SVG paths disappeared during stabilization wait.")



def _extract_svg_payload(driver: webdriver.Chrome) -> Dict[str, Any]:
    script = r'''
const result = {
  pageTitle: document.title || "",
  noData: false,
  candidates: [],
  jsError: null,
};

try {
  const noDataEl = Array.from(document.querySelectorAll('*')).find(
    el => (el.textContent || '').trim() === 'No Data Recorded'
  );
  if (noDataEl) {
    result.noData = true;
  }

  const paths = Array.from(document.querySelectorAll('g.plot.temperature.line path'));
  const allTextNodes = Array.from(document.querySelectorAll('text, svg text'));

  paths.forEach((tempPath, idx) => {
    try {
      const candidate = {
        index: idx,
        pathD: null,
        yTicks: [],
        svgRect: null,
        leftBandMax: null,
      };

      if (!tempPath) {
        result.candidates.push(candidate);
        return;
      }

      candidate.pathD = tempPath.getAttribute('d');
      const svg = tempPath.closest('svg');
      if (!svg) {
        result.candidates.push(candidate);
        return;
      }

      const svgRect = svg.getBoundingClientRect();
      candidate.svgRect = {
        left: svgRect.left,
        right: svgRect.right,
        top: svgRect.top,
        bottom: svgRect.bottom,
        width: svgRect.width,
        height: svgRect.height,
      };

      const pathRect = tempPath.getBoundingClientRect();
      const verticalMin = Math.min(svgRect.top, pathRect.top) - 30;
      const verticalMax = Math.max(svgRect.bottom, pathRect.bottom) + 30;
      const leftBandMax = svgRect.left + Math.min(140, Math.max(90, svgRect.width * 0.22));
      candidate.leftBandMax = leftBandMax - svgRect.left;

      const seen = new Set();
      for (const el of allTextNodes) {
        const raw = (el.textContent || '').trim();
        if (!raw) continue;

        const norm = raw.replace(/°/g, '').replace(/−/g, '-').trim();
        if (!/^[-+]?\d+(?:\.\d+)?$/.test(norm)) continue;

        const rect = el.getBoundingClientRect();
        if (!rect || !isFinite(rect.left) || !isFinite(rect.top)) continue;

        const centerX = rect.left + (rect.width / 2.0);
        const centerY = rect.top + (rect.height / 2.0);
        if (centerY < verticalMin || centerY > verticalMax) continue;
        if (centerX > leftBandMax) continue;

        const relX = centerX - svgRect.left;
        const relY = centerY - svgRect.top;
        const key = `${relX.toFixed(3)}|${relY.toFixed(3)}|${norm}`;
        if (seen.has(key)) continue;
        seen.add(key);

        candidate.yTicks.push({
          x: String(relX),
          y: String(relY),
          text: norm,
        });
      }

      result.candidates.push(candidate);
    } catch (errInner) {
      result.candidates.push({
        index: idx,
        pathD: null,
        yTicks: [],
        svgRect: null,
        leftBandMax: null,
        innerError: String(errInner),
      });
    }
  });
} catch (err) {
  result.jsError = String(err);
}

window.__dbg_last_payload = result;
return result;
'''
    payload = driver.execute_script(script)
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected JS payload for SVG extraction.")
    if payload.get("jsError"):
        raise RuntimeError(f"SVG JS extraction failed: {payload.get('jsError')}")
    return payload



def _parse_path_points(path_d: str) -> List[Tuple[Decimal, Decimal]]:
    tokens = re.findall(r"[ML]\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)", path_d)
    if not tokens:
        raise RuntimeError("Temperature path d=... contains no points.")
    return [(parse_decimal(x, "path.x"), parse_decimal(y, "path.y")) for x, y in tokens]



def _score_tick_run(values: List[Tuple[Decimal, Decimal]], path_min_y: Decimal, path_max_y: Decimal) -> Tuple[int, Decimal, Decimal]:
    if len(values) < 2:
        return (-1, Decimal("999999"), Decimal("999999"))
    y_span = values[-1][0] - values[0][0]
    path_span = path_max_y - path_min_y
    span_gap = abs(y_span - path_span)
    step_ref = values[1][1] - values[0][1]
    step_penalty = Decimal("0")
    for idx in range(2, len(values)):
        step_penalty += abs((values[idx][1] - values[idx - 1][1]) - step_ref)
    return (len(values), -span_gap, -step_penalty)



def _parse_y_ticks(ticks: List[Dict[str, Any]], path_points: List[Tuple[Decimal, Decimal]]) -> List[Tuple[Decimal, Decimal]]:
    parsed: List[Tuple[Decimal, Decimal, Decimal]] = []
    for item in ticks:
        if not isinstance(item, dict):
            continue
        try:
            label_x = parse_decimal(item.get("x"), "tick.x")
            label_y = parse_decimal(item.get("y"), "tick.y")
            label_value = parse_decimal(item.get("text"), "tick.text")
        except Exception:
            continue
        parsed.append((label_x, label_y, label_value))

    if len(parsed) < 2:
        raise RuntimeError(f"Not enough Y-axis ticks to reconstruct temperature. Parsed ticks={parsed!r}")

    path_ys = [y for _, y in path_points]
    path_min_y = min(path_ys)
    path_max_y = max(path_ys)

    clusters: Dict[str, List[Tuple[Decimal, Decimal]]] = {}
    for label_x, label_y, label_value in parsed:
        bucket = str(int(label_x / Decimal("20")))
        clusters.setdefault(bucket, []).append((label_y, label_value))

    best_run: List[Tuple[Decimal, Decimal]] = []
    best_score: Tuple[int, Decimal, Decimal] = (-1, Decimal("-999999"), Decimal("-999999"))
    for values in clusters.values():
        values.sort(key=lambda pair: pair[0])
        deduped: List[Tuple[Decimal, Decimal]] = []
        seen = set()
        for item in values:
            key = (str(item[0]), str(item[1]))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        if len(deduped) < 2:
            continue

        current_run: List[Tuple[Decimal, Decimal]] = [deduped[0]]
        local_candidates: List[List[Tuple[Decimal, Decimal]]] = []
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
                        local_candidates.append(current_run[:])
                        current_run = [current_run[-1], item]
            else:
                local_candidates.append(current_run[:])
                current_run = [item]
        local_candidates.append(current_run[:])

        for candidate in local_candidates:
            score = _score_tick_run(candidate, path_min_y, path_max_y)
            if score > best_score:
                best_score = score
                best_run = candidate[:]

    if len(best_run) < 2:
        raise RuntimeError(f"Not enough Y-axis ticks to reconstruct temperature. Parsed ticks={parsed!r}")
    return best_run



def _infer_unit_from_ticks_only(tick_values: List[Decimal]) -> Optional[str]:
    if not tick_values:
        return None
    max_abs = max(abs(v) for v in tick_values)
    if max_abs >= Decimal("60"):
        return "F"
    if max_abs <= Decimal("35"):
        return "C"
    return None



def _choose_stream_unit(value_raw: Decimal, tick_values: List[Decimal], api_state: Optional[ApiState]) -> Tuple[str, str]:
    by_ticks = _infer_unit_from_ticks_only(tick_values)
    if by_ticks in {"C", "F"}:
        return by_ticks, "tick_range"

    if api_state is not None:
        as_c_delta = abs(value_raw - api_state.value_c)
        as_f_delta = abs(value_raw - api_state.value_f)
        if as_f_delta < as_c_delta:
            return "F", "api_proximity"
        return "C", "api_proximity"

    return "C", "fallback_default"



def _build_ui_state_from_candidate(candidate: Dict[str, Any], page_title: str, api_state: Optional[ApiState]) -> UiState:
    path_d = str(candidate.get("pathD") or "").strip()
    if not path_d:
        raise RuntimeError("Temperature SVG path was not found for candidate.")

    points = _parse_path_points(path_d)
    ticks = _parse_y_ticks(candidate.get("yTicks") or [], points)

    tick_values = [v for _, v in ticks]
    stream_unit, unit_detection_source = _choose_stream_unit(tick_values[0], tick_values, api_state)

    tick_top_y, tick_top_value = ticks[0]
    tick_bottom_y, tick_bottom_value = ticks[-1]
    path_ys = [y for _, y in points]
    path_top_y = min(path_ys)
    path_bottom_y = max(path_ys)
    if path_bottom_y == path_top_y:
        raise RuntimeError("Path Y range is degenerate.")

    last_x, last_y = points[-1]
    ratio = (last_y - path_top_y) / (path_bottom_y - path_top_y)
    value_raw = tick_top_value + (tick_bottom_value - tick_top_value) * ratio

    stream_unit, unit_detection_source = _choose_stream_unit(value_raw, tick_values, api_state)
    if stream_unit == "F":
        value_f = value_raw
        value_c = f_to_c_decimal(value_f)
    else:
        value_c = value_raw
        value_f = c_to_f_decimal(value_c)

    return UiState(
        stream_unit=stream_unit,
        unit_detection_source=unit_detection_source,
        value_raw=value_raw,
        value_c=value_c,
        value_f=value_f,
        source="svg_temperature_line",
        path_last_y=last_y,
        path_last_x=last_x,
        y_axis_top_y=path_top_y,
        y_axis_bottom_y=path_bottom_y,
        y_axis_top_value=tick_top_value,
        y_axis_bottom_value=tick_bottom_value,
        path_point_count=len(points),
        page_title=page_title,
        candidate_index=int(candidate.get("index") or 0),
    )



def read_ui_states(driver: webdriver.Chrome, api_state: Optional[ApiState] = None) -> Dict[str, UiState]:
    driver.get(TARGET_PAGE_URL)
    wait_for_stable_temp_svg(driver)
    payload = _extract_svg_payload(driver)

    if payload.get("noData") and not payload.get("candidates"):
        raise RuntimeError("History UI shows 'No Data Recorded'.")

    page_title = str(payload.get("pageTitle") or "")
    states: Dict[str, UiState] = {}
    candidate_errors: List[str] = []

    for candidate in payload.get("candidates") or []:
        try:
            state = _build_ui_state_from_candidate(candidate, page_title, api_state)
        except Exception as exc:
            candidate_errors.append(f"idx={candidate.get('index')} err={exc}")
            continue
        if state.stream_unit not in states:
            states[state.stream_unit] = state

    if not states:
        raise RuntimeError(f"No usable temperature SVG streams found. Candidate errors: {' | '.join(candidate_errors)}")

    if "C" not in states:
        log("UI_WARN", f"C stream not found on this poll. Candidate errors: {' | '.join(candidate_errors) if candidate_errors else 'n/a'}")
    if "F" not in states:
        log("UI_WARN", f"F stream not found on this poll. Candidate errors: {' | '.join(candidate_errors) if candidate_errors else 'n/a'}")

    return states



def ui_identity(state: UiState) -> str:
    return (
        f"{state.stream_unit}|{fmt_decimal(state.value_raw, 6)}|"
        f"{fmt_decimal(state.path_last_x, 6)}|{fmt_decimal(state.path_last_y, 6)}"
    )


# =========================
# MATCHER
# =========================
def _api_stream_value(api_state: ApiState, stream_unit: str) -> Decimal:
    return api_state.value_c if stream_unit == "C" else api_state.value_f



def _ui_stream_value(ui_state: UiState, stream_unit: str) -> Decimal:
    return ui_state.value_c if stream_unit == "C" else ui_state.value_f



def _stream_tolerance(stream_unit: str) -> Decimal:
    return MATCH_TOLERANCE_C if stream_unit == "C" else MATCH_TOLERANCE_F



def process_api_state(memory: MonitorMemory, state: ApiState) -> None:
    identity = api_identity(state)
    if identity == memory.last_api_identity:
        return

    memory.last_api_identity = identity
    now_epoch = time.time()

    for stream_unit in ("C", "F"):
        stream_value = _api_stream_value(state, stream_unit)
        key = decimal_key(stream_value)
        bucket = memory.pending_by_stream_key[stream_unit]
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = PendingApiValue(
                stream_unit=stream_unit,
                stream_value=stream_value,
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
    if identity == memory.last_ui_identity_by_stream.get(state.stream_unit):
        return

    memory.last_ui_identity_by_stream[state.stream_unit] = identity
    log(
        "UI_NEW",
        (
            f"stream={state.stream_unit} unit_source={state.unit_detection_source} value={fmt_decimal(state.value_raw)}°{state.stream_unit} "
            f"temp_c={fmt_decimal(state.value_c)}°C temp_f={fmt_decimal(state.value_f)}°F "
            f"source={state.source} candidate_index={state.candidate_index} "
            f"path_last=({fmt_decimal(state.path_last_x)},{fmt_decimal(state.path_last_y)}) "
            f"y_scale_top={fmt_decimal(state.y_axis_top_value)}@{fmt_decimal(state.y_axis_top_y)} "
            f"y_scale_bottom={fmt_decimal(state.y_axis_bottom_value)}@{fmt_decimal(state.y_axis_bottom_y)} "
            f"points={state.path_point_count}"
        ),
        {
            "stream_unit": state.stream_unit,
            "unit_detection_source": state.unit_detection_source,
            "value_raw": fmt_decimal(state.value_raw, 6),
            "value_c": fmt_decimal(state.value_c, 6),
            "value_f": fmt_decimal(state.value_f, 6),
            "candidate_index": state.candidate_index,
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
    stream_unit = ui_state.stream_unit
    ui_value = _ui_stream_value(ui_state, stream_unit)
    bucket = memory.pending_by_stream_key[stream_unit]
    exact_key = decimal_key(ui_value)
    pending = bucket.get(exact_key)

    if pending is not None and not pending.matched:
        pending.matched = True
        lag_seconds = max(0.0, time.time() - pending.first_seen_epoch)
        log(
            "MATCH",
            (
                f"stream={stream_unit} value={fmt_decimal(ui_value)}°{stream_unit} "
                f"temp_c={fmt_decimal(ui_state.value_c)}°C temp_f={fmt_decimal(ui_state.value_f)}°F "
                f"lag_seconds={lag_seconds:.1f} api_first_seen={pending.first_seen_wall} "
                f"api_obs_time_local={pending.obs_time_local}"
            ),
            {
                "stream_unit": stream_unit,
                "stream_value": fmt_decimal(ui_value, 6),
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

    unmatched_values = [p for p in bucket.values() if not p.matched]
    if unmatched_values:
        closest = min(unmatched_values, key=lambda p: abs(p.stream_value - ui_value))
        delta = abs(closest.stream_value - ui_value)
        state_label = "WITHIN_TOLERANCE" if delta <= _stream_tolerance(stream_unit) else "WAITING_UI_OR_MISMATCH"
        log(
            "DIFF",
            (
                f"stream={stream_unit} ui_value={fmt_decimal(ui_value)}°{stream_unit} "
                f"ui_temp_c={fmt_decimal(ui_state.value_c)}°C ui_temp_f={fmt_decimal(ui_state.value_f)}°F "
                f"closest_unmatched_api_value={fmt_decimal(closest.stream_value)}°{stream_unit} "
                f"delta={fmt_decimal(delta)}°{stream_unit} state={state_label}"
            ),
            {
                "stream_unit": stream_unit,
                "ui_stream_value": fmt_decimal(ui_value, 6),
                "closest_unmatched_api_stream_value": fmt_decimal(closest.stream_value, 6),
                "delta": fmt_decimal(delta, 6),
                "state": state_label,
            },
        )



def sweep_unmatched_api(memory: MonitorMemory, newest_api_state: ApiState) -> None:
    for stream_unit in ("C", "F"):
        newest_stream_value = _api_stream_value(newest_api_state, stream_unit)
        newest_key = decimal_key(newest_stream_value)
        bucket = memory.pending_by_stream_key[stream_unit]
        to_remove: List[str] = []
        for key, pending in bucket.items():
            if pending.matched:
                continue
            if key != newest_key:
                log(
                    "API_REPLACED_BEFORE_UI",
                    (
                        f"stream={stream_unit} pending_value={fmt_decimal(pending.stream_value)}°{stream_unit} "
                        f"pending_value_c={fmt_decimal(pending.value_c)}°C pending_value_f={fmt_decimal(pending.value_f)}°F "
                        f"api_obs_time_local={pending.obs_time_local} replaced_by_newer_api_before_ui_match=1"
                    ),
                    {
                        "stream_unit": stream_unit,
                        "pending_stream_value": fmt_decimal(pending.stream_value, 6),
                        "pending_value_c": fmt_decimal(pending.value_c, 6),
                        "pending_value_f": fmt_decimal(pending.value_f, 6),
                        "api_obs_time_local": pending.obs_time_local,
                        "api_identity": pending.api_identity,
                    },
                )
                to_remove.append(key)
        for key in to_remove:
            bucket.pop(key, None)


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
    log("CFG", f"MATCH_TOLERANCE_C={MATCH_TOLERANCE_C}")
    log("CFG", f"MATCH_TOLERANCE_F={MATCH_TOLERANCE_F}")
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
                sweep_unmatched_api(memory, api_state)

                if driver is None:
                    log("BROWSER", "Creating Chrome driver...")
                    driver = build_chrome_driver()

                ui_states = read_ui_states(driver, api_state)
                for stream_unit in ("C", "F"):
                    state = ui_states.get(stream_unit)
                    if state is not None:
                        process_ui_state(memory, state)

                if REBUILD_BROWSER_EVERY_N_POLLS > 0 and memory.poll_count % REBUILD_BROWSER_EVERY_N_POLLS == 0:
                    log("BROWSER", f"Rebuilding browser after {REBUILD_BROWSER_EVERY_N_POLLS} polls.")
                    close_driver_safely(driver)
                    driver = None

            except KeyboardInterrupt:
                log("STOP", "Stopped by user.")
                print()
                input("Press Enter to exit...")
                return 0

            except InvalidSessionIdException as exc:
                log("BROWSER_RESET", f"{type(exc).__name__}: browser session lost; recreating driver")
                close_driver_safely(driver)
                driver = None

            except JavascriptException as exc:
                log("BROWSER_JS", f"{type(exc).__name__}: {exc}")

            except WebDriverException as exc:
                log("BROWSER_RESET", f"{type(exc).__name__}: webdriver failure; recreating driver")
                close_driver_safely(driver)
                driver = None

            except (RuntimeError, TimeoutException, requests.RequestException) as exc:
                if isinstance(exc, RuntimeError) and (
                    "Not enough Y-axis ticks" in str(exc)
                    or "SVG JS extraction failed" in str(exc)
                    or "Temperature SVG path" in str(exc)
                    or "No usable temperature SVG streams found" in str(exc)
                ):
                    try:
                        dbg = driver.execute_script(
                            "return {title: document.title || '', lastPayload: window.__dbg_last_payload || null, tickTexts: Array.from(document.querySelectorAll('text, svg text')).map(x => (x.textContent || '').trim()).filter(Boolean).slice(0, 160), pathCount: document.querySelectorAll('g.plot.temperature.line path').length};"
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
