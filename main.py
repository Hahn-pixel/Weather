#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import time

try:
    import requests
except Exception as exc:
    print(f"[FATAL] Failed to import requests: {exc}", flush=True)
    print("[HINT] Run: py -m pip install requests", flush=True)
    print()
    input("Press Enter to exit...")
    raise

try:
    from selenium.common.exceptions import (
        InvalidSessionIdException,
        JavascriptException,
        TimeoutException,
        WebDriverException,
    )
except Exception as exc:
    print(f"[FATAL] Failed to import selenium: {exc}", flush=True)
    print("[HINT] Run: py -m pip install selenium", flush=True)
    print()
    input("Press Enter to exit...")
    raise

from api_client import build_session, fetch_api_bundle, resolve_location_id_from_page_api
from config import (
    CHECK_INTERVAL_SECONDS,
    JSONL_FILE_PATH,
    LOCATION_ID,
    LOG_FILE_PATH,
    LOG_TO_FILE,
    REBUILD_BROWSER_EVERY_N_POLLS,
    TARGET_PAGE_URL,
    WRITE_JSONL_EVENTS,
)
from helpers import derive_history_date_from_url
from log_utils import Logger
from matcher import process_api_state, process_ui_state, sweep_unmatched_api
from models import MonitorMemory
from ui_scraper import build_chrome_driver, close_driver_safely, collect_debug_snapshot, read_ui_states


LOGGER = Logger(LOG_FILE_PATH if LOG_TO_FILE else None, JSONL_FILE_PATH if WRITE_JSONL_EVENTS else None)


def log(tag, message, payload=None):
    LOGGER.write(tag, message, payload)


def main() -> int:
    global LOCATION_ID

    session = build_session()
    date_key = derive_history_date_from_url(TARGET_PAGE_URL)

    if LOCATION_ID is None:
        LOCATION_ID = resolve_location_id_from_page_api(session, TARGET_PAGE_URL)

    driver = None
    memory = MonitorMemory()

    log("CFG", f"TARGET_PAGE_URL={TARGET_PAGE_URL}")
    log("CFG", f"LOCATION_ID={LOCATION_ID} (resolved from page API)")
    log("CFG", f"HISTORY_DATE={date_key}")
    log("CFG", f"CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}")
    log("CFG", f"REBUILD_BROWSER_EVERY_N_POLLS={REBUILD_BROWSER_EVERY_N_POLLS}")

    if LOG_TO_FILE:
        log("CFG", f"LOG_FILE_PATH={LOGGER.text_path}")

    if WRITE_JSONL_EVENTS:
        log("CFG", f"JSONL_FILE_PATH={LOGGER.jsonl_path}")

    try:
        while True:
            try:
                memory.poll_count += 1

                api_bundle = fetch_api_bundle(session, LOCATION_ID, date_key)
                process_api_state(memory, api_bundle, log)
                sweep_unmatched_api(memory, api_bundle, log)

                if driver is None:
                    log("BROWSER", "Creating Chrome driver...")
                    driver = build_chrome_driver()

                ui_states = read_ui_states(driver)

                for stream_unit in ("C", "F"):
                    state = ui_states.get(stream_unit)
                    if state is not None:
                        process_ui_state(memory, state, log)
                    else:
                        log("UI_WARN", f"{stream_unit} stream not found on this poll.", collect_debug_snapshot(driver))

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
                dbg = collect_debug_snapshot(driver)
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