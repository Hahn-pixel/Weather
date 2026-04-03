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
    from selenium.common.exceptions import InvalidSessionIdException, JavascriptException, TimeoutException, WebDriverException
    from selenium.webdriver import ActionChains
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
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
CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True
SHOW_BROWSER_WINDOW = False
PAGE_LOAD_TIMEOUT_SECONDS = 40
CHART_WAIT_TIMEOUT_SECONDS = 25
REBUILD_BROWSER_EVERY_N_POLLS = 120
LOG_TO_FILE = False
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
HOVER_PAUSE_SECONDS = 0.20


# =========================
# DATA MODELS
# =========================
@dataclass
class ApiReading:
    unit: str
    raw_value: Decimal
    obs_time_utc: str
    obs_time_local: str
    valid_time_gmt: int
    observation_count: int


@dataclass
class ApiBundle:
    value_c: Decimal
    value_f: Decimal
    obs_time_utc_c: str
    obs_time_utc_f: str
    obs_time_local_c: str
    obs_time_local_f: str
    valid_time_gmt_c: int
    valid_time_gmt_f: int
    observation_count_c: int
    observation_count_f: int


@dataclass
class UiState:
    stream_unit: str
    source_mode: str
    value_raw: Decimal
    value_c: Decimal
    value_f: Decimal
    legend_text: str
    callout_text: str
    source: str
    chart_index: int
    bar_count: int
    callout_before: str
    callout_after: str
    path_last_y: Optional[Decimal]
    path_last_x: Optional[Decimal]
    y_axis_top_y: Optional[Decimal]
    y_axis_bottom_y: Optional[Decimal]
    y_axis_top_value: Optional[Decimal]
    y_axis_bottom_value: Optional[Decimal]
    path_point_count: int
    page_title: str


@dataclass
class PendingApiValue:
    stream_unit: str
    stream_value: Decimal
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
            obj = {"ts_local": self.now_str(), "tag": tag, "message": message, "payload": payload}
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



def fmt_decimal(value: Decimal, places: int = 3) -> str:
    q = Decimal("1").scaleb(-places)
    return f"{value.quantize(q):f}"



def decimal_key(value: Decimal) -> str:
    return fmt_decimal(value, 6)



def current_date_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")



def derive_history_date_from_url(url: str) -> str:
    match = re.search(r"/date/(\d{4})-(\d{1,2})-(\d{1,2})(?:/|$)", url, re.IGNORECASE)
    if not match:
        return current_date_yyyymmdd()
    return f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"



def derive_location_id_from_url(url: str) -> str:
    match = re.search(r"/history/daily/([a-z]{2})/.+?/([A-Z0-9]{3,5})(?:/|$)", url, re.IGNORECASE)
    if not match:
        raise RuntimeError(f"Cannot derive station from URL: {url}")
    country = match.group(1).upper()
    station = match.group(2).upper()
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
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)
    return derive_location_id_from_url(page_url)



def epoch_to_utc_str(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")



def parse_callout_value(text: str, expected_unit: str) -> Optional[Decimal]:
    normalized = str(text or "").replace("\xa0", " ").strip()
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°\s*([CF])", normalized, re.IGNORECASE)
    if not match:
        return None
    found_unit = match.group(2).upper()
    if found_unit != expected_unit.upper():
        return None
    return parse_decimal(match.group(1), f"callout_{expected_unit.lower()}")



def _stream_tolerance(stream_unit: str) -> Decimal:
    return MATCH_TOLERANCE_C if stream_unit == "C" else MATCH_TOLERANCE_F


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



def fetch_historical_json(session: requests.Session, units: str) -> Dict[str, Any]:
    date_key = derive_history_date_from_url(TARGET_PAGE_URL)
    response = session.get(
        historical_url(LOCATION_ID),
        params={"apiKey": API_KEY, "units": units, "startDate": date_key, "endDate": date_key},
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
    response.raise_for_status()
    return response.json()



def extract_api_reading(data: Dict[str, Any], units: str) -> ApiReading:
    observations = data.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError(f"Historical API returned no observations for units={units!r}.")
    observation = observations[-1]
    if not isinstance(observation, dict) or "temp" not in observation:
        raise RuntimeError(f"Last observation has no temp field for units={units!r}.")
    return ApiReading(
        unit="F" if units.lower() == "e" else "C",
        raw_value=parse_decimal(observation["temp"], f"api.temp.{units}"),
        obs_time_utc=str(observation.get("valid_time_gmt") or ""),
        obs_time_local=str(observation.get("obs_time_local") or observation.get("valid_time_local") or ""),
        valid_time_gmt=int(observation.get("valid_time_gmt") or 0),
        observation_count=len(observations),
    )



def fetch_api_bundle(session: requests.Session) -> ApiBundle:
    data_c = fetch_historical_json(session, "m")
    data_f = fetch_historical_json(session, "e")
    reading_c = extract_api_reading(data_c, "m")
    reading_f = extract_api_reading(data_f, "e")
    return ApiBundle(
        value_c=reading_c.raw_value,
        value_f=reading_f.raw_value,
        obs_time_utc_c=reading_c.obs_time_utc,
        obs_time_utc_f=reading_f.obs_time_utc,
        obs_time_local_c=reading_c.obs_time_local,
        obs_time_local_f=reading_f.obs_time_local,
        valid_time_gmt_c=reading_c.valid_time_gmt,
        valid_time_gmt_f=reading_f.valid_time_gmt,
        observation_count_c=reading_c.observation_count,
        observation_count_f=reading_f.observation_count,
    )



def api_identity(bundle: ApiBundle) -> str:
    return (
        f"c:{bundle.valid_time_gmt_c}|{fmt_decimal(bundle.value_c, 6)}|{bundle.obs_time_local_c}"
        f"|f:{bundle.valid_time_gmt_f}|{fmt_decimal(bundle.value_f, 6)}|{bundle.obs_time_local_f}"
    )


# =========================
# UI SIDE
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



def wait_for_temperature_charts(driver: webdriver.Chrome) -> None:
    WebDriverWait(driver, CHART_WAIT_TIMEOUT_SECONDS).until(
        lambda d: d.execute_script(
            "return Array.from(document.querySelectorAll('div.legend')).some(x => /Temperature/i.test(x.textContent || ''));"
        )
    )
    time.sleep(0.35)



def _temperature_chart_containers(driver: webdriver.Chrome) -> List[Any]:
    candidates = driver.find_elements(By.CSS_SELECTOR, "lib-wu-chart .charts-canvas > div")
    output: List[Any] = []
    for element in candidates:
        try:
            legend_defs = element.find_elements(By.CSS_SELECTOR, ".legend .legend-def")
            legend_text = " | ".join((x.text or "").strip() for x in legend_defs if (x.text or "").strip())
            if "Temperature" in legend_text:
                output.append(element)
        except Exception:
            continue
    return output



def _container_debug_snapshot(container: Any) -> Dict[str, Any]:
    try:
        legend_defs = container.find_elements(By.CSS_SELECTOR, ".legend .legend-def")
        legend_text = " | ".join((x.text or "").strip() for x in legend_defs if (x.text or "").strip())
    except Exception:
        legend_text = ""
    try:
        local_callout = container.find_element(By.CSS_SELECTOR, ".callout.temperature .callout-text").text.strip()
    except Exception:
        local_callout = ""
    try:
        bar_count = len(container.find_elements(By.CSS_SELECTOR, "rect.bc-bar"))
    except Exception:
        bar_count = 0
    return {"legend": legend_text, "local_callout": local_callout, "bar_count": bar_count}



def _activate_temperature_chart_callouts(driver: webdriver.Chrome) -> None:
    charts = _temperature_chart_containers(driver)
    hover_debug: List[Dict[str, Any]] = []
    for idx, container in enumerate(charts):
        snap_before = _container_debug_snapshot(container)
        try:
            bars = container.find_elements(By.CSS_SELECTOR, "rect.bc-bar")
            if not bars:
                hover_debug.append({"idx": idx, **snap_before, "status": "no_bars"})
                continue
            target = bars[-2] if len(bars) >= 2 else bars[-1]
            ActionChains(driver).move_to_element(target).pause(HOVER_PAUSE_SECONDS).perform()
            time.sleep(HOVER_PAUSE_SECONDS)
            snap_after = _container_debug_snapshot(container)
            hover_debug.append(
                {
                    "idx": idx,
                    "legend": snap_before["legend"],
                    "callout_before": snap_before["local_callout"],
                    "callout_after": snap_after["local_callout"],
                    "bar_count": snap_after["bar_count"],
                    "status": "ok",
                }
            )
        except Exception as exc:
            hover_debug.append({"idx": idx, **snap_before, "status": f"hover_failed:{type(exc).__name__}:{exc}"})
    try:
        driver.execute_script("window.__dbg_hover_debug = arguments[0];", hover_debug)
    except Exception:
        pass



def _extract_temperature_chart_payload(driver: webdriver.Chrome) -> Dict[str, Any]:
    script = r'''
const result = { pageTitle: document.title || '', noData: false, charts: [], jsError: null, hoverDebug: window.__dbg_hover_debug || null };
try {
  const noDataEl = Array.from(document.querySelectorAll('*')).find(el => (el.textContent || '').trim() === 'No Data Recorded');
  if (noDataEl) result.noData = true;

  const chartDivs = Array.from(document.querySelectorAll('lib-wu-chart .charts-canvas > div'));
  chartDivs.forEach((container, idx) => {
    try {
      const legendDefs = Array.from(container.querySelectorAll('.legend .legend-def'));
      const legendTexts = legendDefs.map(x => (x.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
      const temperatureLegend = legendTexts.find(x => /Temperature/i.test(x)) || '';
      if (!temperatureLegend) return;

      const calloutEl = container.querySelector('.callout.temperature .callout-text');
      const calloutText = calloutEl ? ((calloutEl.textContent || '').replace(/\s+/g, ' ').trim()) : '';

      const yTickEls = Array.from(container.querySelectorAll('.y-axis-ticks .tick-label'));
      const yTicks = [];
      yTickEls.forEach(el => {
        const txt = (el.textContent || '').trim();
        if (!txt) return;
        const norm = txt.replace(/°/g, '').replace(/−/g, '-').trim();
        if (!/^[-+]?\d+(?:\.\d+)?$/.test(norm)) return;
        const rect = el.getBoundingClientRect();
        const containerRect = container.getBoundingClientRect();
        yTicks.push({ text: norm, x: String(rect.left - containerRect.left), y: String(rect.top - containerRect.top + rect.height / 2.0) });
      });

      const tempPath = container.querySelector('g.plot.temperature.line path');
      const pathD = tempPath ? (tempPath.getAttribute('d') || '') : '';
      const bars = Array.from(container.querySelectorAll('rect.bc-bar'));

      result.charts.push({
        chartIndex: idx,
        legendText: temperatureLegend,
        calloutText: calloutText,
        yTicks: yTicks,
        pathD: pathD,
        barCount: bars.length,
        innerError: null,
      });
    } catch (errInner) {
      result.charts.push({ chartIndex: idx, legendText: '', calloutText: '', yTicks: [], pathD: '', barCount: 0, innerError: String(errInner) });
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
        raise RuntimeError("Unexpected JS payload for temperature chart extraction.")
    if payload.get("jsError"):
        raise RuntimeError(f"Temperature chart JS extraction failed: {payload.get('jsError')}")
    return payload



def _parse_path_points(path_d: str) -> List[Tuple[Decimal, Decimal]]:
    tokens = re.findall(r"[ML]\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)", str(path_d or ""))
    if not tokens:
        raise RuntimeError("Temperature path d=... contains no points.")
    return [(parse_decimal(x, "path.x"), parse_decimal(y, "path.y")) for x, y in tokens]



def _parse_local_ticks(ticks: List[Dict[str, Any]]) -> List[Tuple[Decimal, Decimal]]:
    parsed: List[Tuple[Decimal, Decimal]] = []
    for item in ticks:
        if not isinstance(item, dict):
            continue
        try:
            label_value = parse_decimal(item.get("text"), "tick.text")
            label_y = parse_decimal(item.get("y"), "tick.y")
        except Exception:
            continue
        parsed.append((label_y, label_value))
    parsed.sort(key=lambda pair: pair[0])
    deduped: List[Tuple[Decimal, Decimal]] = []
    seen = set()
    for pair in parsed:
        key = (str(pair[0]), str(pair[1]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pair)
    if len(deduped) < 2:
        raise RuntimeError(f"Not enough local Y-axis ticks to reconstruct temperature: {ticks!r}")
    return deduped



def _infer_stream_unit_from_legend(legend_text: str) -> Optional[str]:
    normalized = str(legend_text or "").upper()
    if "°C" in normalized or "(C)" in normalized:
        return "C"
    if "°F" in normalized or "(F)" in normalized:
        return "F"
    match = re.search(r"TEMPERATURE\s*\(([^)]+)\)", normalized)
    if not match:
        return None
    payload = match.group(1)
    if "C" in payload:
        return "C"
    if "F" in payload:
        return "F"
    return None



def _build_ui_state_from_chart(chart: Dict[str, Any], page_title: str) -> UiState:
    legend_text = str(chart.get("legendText") or "").strip()
    stream_unit = _infer_stream_unit_from_legend(legend_text)
    if stream_unit not in {"C", "F"}:
        raise RuntimeError(f"Cannot infer stream unit from legend: {legend_text!r}")

    callout_text = str(chart.get("calloutText") or "").strip()
    callout_value = parse_callout_value(callout_text, stream_unit)

    if callout_value is not None:
        value_raw = callout_value
        source_mode = "callout"
        path_last_x = None
        path_last_y = None
        y_axis_top_y = None
        y_axis_bottom_y = None
        y_axis_top_value = None
        y_axis_bottom_value = None
        path_point_count = 0
    else:
        path_points = _parse_path_points(chart.get("pathD") or "")
        ticks = _parse_local_ticks(chart.get("yTicks") or [])
        tick_top_y, tick_top_value = ticks[0]
        tick_bottom_y, tick_bottom_value = ticks[-1]
        path_ys = [y for _, y in path_points]
        path_top_y = min(path_ys)
        path_bottom_y = max(path_ys)
        if path_bottom_y == path_top_y:
            raise RuntimeError("Path Y range is degenerate.")
        last_x, last_y = path_points[-1]
        ratio = (last_y - path_top_y) / (path_bottom_y - path_top_y)
        value_raw = tick_top_value + (tick_bottom_value - tick_top_value) * ratio
        source_mode = "svg_fallback"
        path_last_x = last_x
        path_last_y = last_y
        y_axis_top_y = path_top_y
        y_axis_bottom_y = path_bottom_y
        y_axis_top_value = tick_top_value
        y_axis_bottom_value = tick_bottom_value
        path_point_count = len(path_points)

    value_c = value_raw if stream_unit == "C" else Decimal("NaN")
    value_f = value_raw if stream_unit == "F" else Decimal("NaN")

    return UiState(
        stream_unit=stream_unit,
        source_mode=source_mode,
        value_raw=value_raw,
        value_c=value_c,
        value_f=value_f,
        legend_text=legend_text,
        callout_text=callout_text,
        source="temperature_chart",
        chart_index=int(chart.get("chartIndex") or 0),
        bar_count=int(chart.get("barCount") or 0),
        callout_before="",
        callout_after=callout_text,
        path_last_y=path_last_y,
        path_last_x=path_last_x,
        y_axis_top_y=y_axis_top_y,
        y_axis_bottom_y=y_axis_bottom_y,
        y_axis_top_value=y_axis_top_value,
        y_axis_bottom_value=y_axis_bottom_value,
        path_point_count=path_point_count,
        page_title=page_title,
    )



def read_ui_states(driver: webdriver.Chrome) -> Dict[str, UiState]:
    driver.get(TARGET_PAGE_URL)
    wait_for_temperature_charts(driver)
    _activate_temperature_chart_callouts(driver)
    payload = _extract_temperature_chart_payload(driver)
    if payload.get("noData") and not payload.get("charts"):
        raise RuntimeError("History UI shows 'No Data Recorded'.")
    page_title = str(payload.get("pageTitle") or "")
    states: Dict[str, UiState] = {}
    chart_errors: List[str] = []
    for chart in payload.get("charts") or []:
        try:
            state = _build_ui_state_from_chart(chart, page_title)
        except Exception as exc:
            chart_errors.append(f"idx={chart.get('chartIndex')} legend={chart.get('legendText')!r} bar_count={chart.get('barCount')} callout={chart.get('calloutText')!r} err={exc}")
            continue
        states[state.stream_unit] = state
    if not states:
        raise RuntimeError(f"No usable temperature charts found. Chart errors: {' | '.join(chart_errors)}")
    if "C" not in states:
        log("UI_WARN", f"C stream not found on this poll. Chart errors: {' | '.join(chart_errors) if chart_errors else 'n/a'}")
    if "F" not in states:
        log("UI_WARN", f"F stream not found on this poll. Chart errors: {' | '.join(chart_errors) if chart_errors else 'n/a'}")
    return states



def ui_identity(state: UiState) -> str:
    return f"{state.stream_unit}|{state.source_mode}|{fmt_decimal(state.value_raw, 6)}|{state.chart_index}|{state.callout_text}"


# =========================
# MATCHER
# =========================
def _api_stream_value(bundle: ApiBundle, stream_unit: str) -> Decimal:
    return bundle.value_c if stream_unit == "C" else bundle.value_f



def _api_stream_meta(bundle: ApiBundle, stream_unit: str) -> Tuple[str, int]:
    if stream_unit == "C":
        return bundle.obs_time_local_c, bundle.valid_time_gmt_c
    return bundle.obs_time_local_f, bundle.valid_time_gmt_f



def process_api_state(memory: MonitorMemory, bundle: ApiBundle) -> None:
    identity = api_identity(bundle)
    if identity == memory.last_api_identity:
        return
    memory.last_api_identity = identity
    now_epoch = time.time()
    for stream_unit in ("C", "F"):
        stream_value = _api_stream_value(bundle, stream_unit)
        obs_time_local, valid_time_gmt = _api_stream_meta(bundle, stream_unit)
        key = decimal_key(stream_value)
        bucket = memory.pending_by_stream_key[stream_unit]
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = PendingApiValue(
                stream_unit=stream_unit,
                stream_value=stream_value,
                first_seen_epoch=now_epoch,
                first_seen_wall=LOGGER.now_str(),
                api_identity=identity,
                obs_time_local=obs_time_local,
                valid_time_gmt=valid_time_gmt,
                matched=False,
            )
        else:
            existing.api_identity = identity
            existing.obs_time_local = obs_time_local
            existing.valid_time_gmt = valid_time_gmt
    log(
        "API_NEW",
        (
            f"temp_c={fmt_decimal(bundle.value_c)}°C temp_f={fmt_decimal(bundle.value_f)}°F "
            f"valid_time_gmt_c={bundle.valid_time_gmt_c} ({epoch_to_utc_str(bundle.valid_time_gmt_c) if bundle.valid_time_gmt_c else 'n/a'}) "
            f"valid_time_gmt_f={bundle.valid_time_gmt_f} ({epoch_to_utc_str(bundle.valid_time_gmt_f) if bundle.valid_time_gmt_f else 'n/a'}) "
            f"obs_time_local_c={bundle.obs_time_local_c} obs_time_local_f={bundle.obs_time_local_f} "
            f"observations_c={bundle.observation_count_c} observations_f={bundle.observation_count_f}"
        ),
        {
            "value_c": fmt_decimal(bundle.value_c, 6),
            "value_f": fmt_decimal(bundle.value_f, 6),
            "valid_time_gmt_c": bundle.valid_time_gmt_c,
            "valid_time_gmt_f": bundle.valid_time_gmt_f,
            "obs_time_local_c": bundle.obs_time_local_c,
            "obs_time_local_f": bundle.obs_time_local_f,
            "observation_count_c": bundle.observation_count_c,
            "observation_count_f": bundle.observation_count_f,
            "api_identity": identity,
        },
    )



def process_ui_state(memory: MonitorMemory, state: UiState) -> None:
    identity = ui_identity(state)
    if identity == memory.last_ui_identity_by_stream.get(state.stream_unit):
        return
    memory.last_ui_identity_by_stream[state.stream_unit] = identity
    extra = ""
    if state.source_mode == "svg_fallback" and state.path_last_x is not None and state.path_last_y is not None:
        extra = (
            f" path_last=({fmt_decimal(state.path_last_x)},{fmt_decimal(state.path_last_y)})"
            f" y_scale_top={fmt_decimal(state.y_axis_top_value or Decimal('0'))}@{fmt_decimal(state.y_axis_top_y or Decimal('0'))}"
            f" y_scale_bottom={fmt_decimal(state.y_axis_bottom_value or Decimal('0'))}@{fmt_decimal(state.y_axis_bottom_y or Decimal('0'))}"
            f" points={state.path_point_count}"
        )
    log(
        "UI_NEW",
        (
            f"stream={state.stream_unit} source_mode={state.source_mode} value={fmt_decimal(state.value_raw)}°{state.stream_unit}"
            f" legend={state.legend_text!r} callout={state.callout_text!r} chart_index={state.chart_index} bar_count={state.bar_count}{extra}"
        ),
        {
            "stream_unit": state.stream_unit,
            "source_mode": state.source_mode,
            "value_raw": fmt_decimal(state.value_raw, 6),
            "legend_text": state.legend_text,
            "callout_text": state.callout_text,
            "chart_index": state.chart_index,
            "bar_count": state.bar_count,
            "path_last_x": fmt_decimal(state.path_last_x, 6) if state.path_last_x is not None else None,
            "path_last_y": fmt_decimal(state.path_last_y, 6) if state.path_last_y is not None else None,
            "y_axis_top_value": fmt_decimal(state.y_axis_top_value, 6) if state.y_axis_top_value is not None else None,
            "y_axis_bottom_value": fmt_decimal(state.y_axis_bottom_value, 6) if state.y_axis_bottom_value is not None else None,
            "path_point_count": state.path_point_count,
            "ui_identity": identity,
        },
    )
    evaluate_matches(memory, state)



def evaluate_matches(memory: MonitorMemory, ui_state: UiState) -> None:
    stream_unit = ui_state.stream_unit
    ui_value = ui_state.value_raw
    bucket = memory.pending_by_stream_key[stream_unit]
    exact_key = decimal_key(ui_value)
    pending = bucket.get(exact_key)
    if pending is not None and not pending.matched:
        pending.matched = True
        lag_seconds = max(0.0, time.time() - pending.first_seen_epoch)
        log(
            "MATCH",
            (
                f"stream={stream_unit} value={fmt_decimal(ui_value)}°{stream_unit} lag_seconds={lag_seconds:.1f}"
                f" api_first_seen={pending.first_seen_wall} api_obs_time_local={pending.obs_time_local}"
            ),
            {
                "stream_unit": stream_unit,
                "stream_value": fmt_decimal(ui_value, 6),
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
                f"stream={stream_unit} ui_value={fmt_decimal(ui_value)}°{stream_unit}"
                f" closest_unmatched_api_value={fmt_decimal(closest.stream_value)}°{stream_unit}"
                f" delta={fmt_decimal(delta)}°{stream_unit} state={state_label}"
            ),
            {
                "stream_unit": stream_unit,
                "ui_stream_value": fmt_decimal(ui_value, 6),
                "closest_unmatched_api_stream_value": fmt_decimal(closest.stream_value, 6),
                "delta": fmt_decimal(delta, 6),
                "state": state_label,
            },
        )



def sweep_unmatched_api(memory: MonitorMemory, newest_api_bundle: ApiBundle) -> None:
    for stream_unit in ("C", "F"):
        newest_stream_value = _api_stream_value(newest_api_bundle, stream_unit)
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
                        f"stream={stream_unit} pending_value={fmt_decimal(pending.stream_value)}°{stream_unit}"
                        f" api_obs_time_local={pending.obs_time_local} replaced_by_newer_api_before_ui_match=1"
                    ),
                    {
                        "stream_unit": stream_unit,
                        "pending_stream_value": fmt_decimal(pending.stream_value, 6),
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
                api_bundle = fetch_api_bundle(session)
                process_api_state(memory, api_bundle)
                sweep_unmatched_api(memory, api_bundle)
                if driver is None:
                    log("BROWSER", "Creating Chrome driver...")
                    driver = build_chrome_driver()
                ui_states = read_ui_states(driver)
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
                try:
                    dbg = driver.execute_script(
                        "return {title: document.title || '', lastPayload: window.__dbg_last_payload || null, hoverDebug: window.__dbg_hover_debug || null, callouts: Array.from(document.querySelectorAll('.callout.temperature .callout-text')).map(x => (x.textContent || '').trim()), legends: Array.from(document.querySelectorAll('.legend .legend-def')).map(x => (x.textContent || '').replace(/\\s+/g, ' ').trim())};"
                    ) if driver is not None else {}
                except Exception:
                    dbg = {}
                log("ERR", f"{type(exc).__name__}: {exc} | debug={dbg}")
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
