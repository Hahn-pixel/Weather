#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
weather_page_temperature_monitor.py

Мінімальний монітор для буквально намальованої точки графіка History.

Логіка:
- читає historical endpoint Weather Underground / weather.com;
- бере останній observation.temp;
- це і є остання намальована точка графіка;
- опційно шле alert при будь-якій зміні;
- шле alert при strict upward crossing порога.

Залежність:
    py -m pip install requests
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

try:
    import requests
except Exception as exc:
    print(f"[FATAL] Не вдалося імпортувати requests: {exc}", flush=True)
    print("[HINT] Виконайте: py -m pip install requests", flush=True)
    input("\nPress Enter to exit...")
    raise


# =========================
# CONFIG
# =========================
TARGET_PAGE_URL = "https://www.wunderground.com/history/daily/ca/mississauga/CYYZ"
LOCATION_ID = "CYYZ:9:CA"
API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"
API_UNITS = "m"                  # 'm' = Celsius, 'e' = Fahrenheit
TEMP_THRESHOLD_UNIT = "C"        # 'C' або 'F'
TEMP_THRESHOLD = "1.5"           # alert only if current > threshold
CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True
PRINT_HEARTBEAT = True
ALERT_ON_ANY_CHANGE = True
POPUP_ON_ANY_CHANGE = True
BEEP_ON_ANY_CHANGE = True
POPUP_ON_THRESHOLD_CROSS = True
BEEP_ON_THRESHOLD_CROSS = True
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
REFERER = "https://www.wunderground.com/"


@dataclass
class ThresholdConfig:
    unit: str
    threshold_raw: Decimal
    threshold_c: Decimal
    threshold_f: Decimal


@dataclass
class ObservationState:
    raw_value: Decimal
    raw_unit: str
    value_c: Decimal
    value_f: Decimal
    obs_time_utc: str
    obs_time_local: str
    observation_count: int


@dataclass
class MonitorMemory:
    initialized: bool = False
    last_state: Optional[ObservationState] = None
    last_above: Optional[bool] = None
    last_identity: Optional[str] = None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(tag: str, message: str) -> None:
    print(f"[{now_str()}] [{tag}] {message}", flush=True)


def parse_decimal(value: str, field_name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Некоректне значення {field_name}: {value!r}") from exc


def normalize_threshold_unit(value: str) -> str:
    unit = str(value).strip().upper()
    if unit not in {"C", "F"}:
        raise RuntimeError(f"Некоректний TEMP_THRESHOLD_UNIT: {value!r}. Дозволено тільки 'C' або 'F'.")
    return unit


def f_to_c_decimal(value_f: Decimal) -> Decimal:
    return (value_f - Decimal("32")) * Decimal("5") / Decimal("9")


def c_to_f_decimal(value_c: Decimal) -> Decimal:
    return (value_c * Decimal("9") / Decimal("5")) + Decimal("32")


def fmt_decimal(value: Decimal, places: int = 3) -> str:
    q = Decimal("1").scaleb(-places)
    return f"{value.quantize(q):f}"


def build_threshold_config() -> ThresholdConfig:
    unit = normalize_threshold_unit(TEMP_THRESHOLD_UNIT)
    threshold_raw = parse_decimal(TEMP_THRESHOLD, "TEMP_THRESHOLD")
    if unit == "C":
        threshold_c = threshold_raw
        threshold_f = c_to_f_decimal(threshold_raw)
    else:
        threshold_f = threshold_raw
        threshold_c = f_to_c_decimal(threshold_raw)
    return ThresholdConfig(unit=unit, threshold_raw=threshold_raw, threshold_c=threshold_c, threshold_f=threshold_f)


def beep_and_popup(title: str, message: str, *, beep_enabled: bool, popup_enabled: bool) -> None:
    if beep_enabled:
        try:
            import winsound
            winsound.Beep(1100, 200)
            winsound.Beep(1500, 250)
            winsound.Beep(1900, 300)
        except Exception:
            print("\a", end="", flush=True)
    if popup_enabled:
        try:
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x00001000)
        except Exception:
            pass


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


def current_date_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def fetch_historical_json(session: requests.Session) -> Dict[str, Any]:
    date_key = current_date_yyyymmdd()
    params = {
        "apiKey": API_KEY,
        "units": API_UNITS,
        "startDate": date_key,
        "endDate": date_key,
    }
    response = session.get(historical_url(LOCATION_ID), params=params, timeout=REQUEST_TIMEOUT_SECONDS, verify=VERIFY_TLS)
    response.raise_for_status()
    return response.json()


def extract_last_observation(data: Dict[str, Any]) -> ObservationState:
    observations = data.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RuntimeError("Historical API не повернув observations.")

    obs = observations[-1]
    if not isinstance(obs, dict) or "temp" not in obs:
        raise RuntimeError("Останнє observation не містить temp.")

    raw_value = Decimal(str(obs["temp"]))
    raw_unit = "F" if API_UNITS.lower() == "e" else "C"

    if raw_unit == "F":
        value_f = raw_value
        value_c = f_to_c_decimal(raw_value)
    else:
        value_c = raw_value
        value_f = c_to_f_decimal(raw_value)

    return ObservationState(
        raw_value=raw_value,
        raw_unit=raw_unit,
        value_c=value_c,
        value_f=value_f,
        obs_time_utc=str(obs.get("valid_time_gmt") or ""),
        obs_time_local=str(obs.get("obs_time_local") or obs.get("valid_time_local") or ""),
        observation_count=len(observations),
    )


def observation_identity(state: ObservationState) -> str:
    return f"{state.obs_time_utc}|{state.obs_time_local}|{state.raw_value}"


def current_value_in_threshold_unit(state: ObservationState, threshold_cfg: ThresholdConfig) -> Decimal:
    return state.value_c if threshold_cfg.unit == "C" else state.value_f


def is_strictly_above_threshold(state: ObservationState, threshold_cfg: ThresholdConfig) -> bool:
    return current_value_in_threshold_unit(state, threshold_cfg) > threshold_cfg.threshold_raw


def format_state_for_log(state: ObservationState, threshold_cfg: ThresholdConfig, freshness: str) -> str:
    current_threshold_unit_value = current_value_in_threshold_unit(state, threshold_cfg)
    cmp_flag = "ABOVE" if current_threshold_unit_value > threshold_cfg.threshold_raw else "AT_OR_BELOW"
    return (
        f"value={fmt_decimal(state.raw_value)}°{state.raw_unit} "
        f"temp_c={fmt_decimal(state.value_c)}°C "
        f"temp_f={fmt_decimal(state.value_f)}°F "
        f"threshold={fmt_decimal(threshold_cfg.threshold_raw)}°{threshold_cfg.unit} "
        f"current_in_threshold_unit={fmt_decimal(current_threshold_unit_value)}°{threshold_cfg.unit} "
        f"state={cmp_flag} "
        f"obs_time_local={state.obs_time_local} "
        f"observations={state.observation_count} "
        f"freshness={freshness}"
    )


def build_popup_message(title_prefix: str, state: ObservationState, threshold_cfg: ThresholdConfig, previous_state: Optional[ObservationState]) -> str:
    lines = [
        TARGET_PAGE_URL,
        "",
        title_prefix,
        f"Current value: {fmt_decimal(current_value_in_threshold_unit(state, threshold_cfg))}°{threshold_cfg.unit}",
        f"Threshold: {fmt_decimal(threshold_cfg.threshold_raw)}°{threshold_cfg.unit}",
        f"Raw value: {fmt_decimal(state.raw_value)}°{state.raw_unit}",
        f"Celsius: {fmt_decimal(state.value_c)}°C",
        f"Fahrenheit: {fmt_decimal(state.value_f)}°F",
        f"obs_time_local: {state.obs_time_local}",
    ]
    if previous_state is not None:
        lines.insert(3, f"Previous value: {fmt_decimal(current_value_in_threshold_unit(previous_state, threshold_cfg))}°{threshold_cfg.unit}")
    return "\n".join(lines)


def main() -> int:
    threshold_cfg = build_threshold_config()
    log("CFG", f"TARGET_PAGE_URL={TARGET_PAGE_URL}")
    log("CFG", f"LOCATION_ID={LOCATION_ID}")
    log("CFG", f"API_UNITS={API_UNITS}")
    log("CFG", f"TEMP_THRESHOLD={fmt_decimal(threshold_cfg.threshold_raw)}°{threshold_cfg.unit}")
    log("CFG", f"CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}")
    log("CFG", f"ALERT_ON_ANY_CHANGE={ALERT_ON_ANY_CHANGE}")

    session = build_session()
    memory = MonitorMemory()

    while True:
        try:
            data = fetch_historical_json(session)
            state = extract_last_observation(data)
            identity = observation_identity(state)
            is_above = is_strictly_above_threshold(state, threshold_cfg)

            if not memory.initialized:
                memory.initialized = True
                memory.last_state = state
                memory.last_above = is_above
                memory.last_identity = identity
                log("INIT", format_state_for_log(state, threshold_cfg, "INIT"))

                if is_above:
                    log("CROSS", f"Поріг уже перевищено на старті: {fmt_decimal(current_value_in_threshold_unit(state, threshold_cfg))}°{threshold_cfg.unit} > {fmt_decimal(threshold_cfg.threshold_raw)}°{threshold_cfg.unit}")
                    beep_and_popup(
                        "Weather threshold exceeded",
                        build_popup_message("Threshold already exceeded at startup.", state, threshold_cfg, None),
                        beep_enabled=BEEP_ON_THRESHOLD_CROSS,
                        popup_enabled=POPUP_ON_THRESHOLD_CROSS,
                    )
            else:
                prev_state = memory.last_state
                prev_flag = memory.last_above
                prev_identity = memory.last_identity
                assert prev_state is not None and prev_flag is not None and prev_identity is not None

                freshness = "NEW_OBS" if identity != prev_identity else "SAME_OBS"
                if PRINT_HEARTBEAT:
                    log("POLL", format_state_for_log(state, threshold_cfg, freshness))

                if ALERT_ON_ANY_CHANGE and state.raw_value != prev_state.raw_value:
                    log("CHANGE", f"value changed {fmt_decimal(prev_state.raw_value)}°{prev_state.raw_unit} -> {fmt_decimal(state.raw_value)}°{state.raw_unit}")
                    beep_and_popup(
                        "Weather value changed",
                        build_popup_message("Chart value changed.", state, threshold_cfg, prev_state),
                        beep_enabled=BEEP_ON_ANY_CHANGE,
                        popup_enabled=POPUP_ON_ANY_CHANGE,
                    )

                crossed_up = (not prev_flag) and is_above
                reset_back = prev_flag and (not is_above)

                if crossed_up:
                    log("CROSS", f"value {fmt_decimal(current_value_in_threshold_unit(prev_state, threshold_cfg))}°{threshold_cfg.unit} -> {fmt_decimal(current_value_in_threshold_unit(state, threshold_cfg))}°{threshold_cfg.unit} exceeded threshold {fmt_decimal(threshold_cfg.threshold_raw)}°{threshold_cfg.unit}")
                    beep_and_popup(
                        "Weather threshold exceeded",
                        build_popup_message("Threshold crossed upward.", state, threshold_cfg, prev_state),
                        beep_enabled=BEEP_ON_THRESHOLD_CROSS,
                        popup_enabled=POPUP_ON_THRESHOLD_CROSS,
                    )
                elif reset_back:
                    log("RESET", f"value {fmt_decimal(current_value_in_threshold_unit(prev_state, threshold_cfg))}°{threshold_cfg.unit} -> {fmt_decimal(current_value_in_threshold_unit(state, threshold_cfg))}°{threshold_cfg.unit} returned to threshold-or-below zone {fmt_decimal(threshold_cfg.threshold_raw)}°{threshold_cfg.unit}")

                memory.last_state = state
                memory.last_above = is_above
                memory.last_identity = identity

        except KeyboardInterrupt:
            log("STOP", "Зупинено користувачем.")
            print()
            input("Press Enter to exit...")
            return 0
        except Exception as exc:
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
        print(f"[FATAL] {type(exc).__name__}: {exc}", flush=True)
        print()
        input("Press Enter to exit...")
        raise
