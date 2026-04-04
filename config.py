#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from decimal import Decimal

TARGET_PAGE_URL = "https://www.wunderground.com/history/daily/us/tx/houston/KHOU/date/2026-4-4"
LOCATION_ID = None
API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

CHECK_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 20
VERIFY_TLS = True

SHOW_BROWSER_WINDOW = False
PAGE_LOAD_TIMEOUT_SECONDS = 40
CHART_WAIT_TIMEOUT_SECONDS = 25
REBUILD_BROWSER_EVERY_N_POLLS = 120

MATCH_TOLERANCE_C = Decimal("0.001")
MATCH_TOLERANCE_F = Decimal("0.001")

HOVER_PAUSE_SECONDS = 0.20
CHART_ACTIVATION_ATTEMPTS = 3
CHART_READY_RETRY_DELAY_SECONDS = 0.35

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