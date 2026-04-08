#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

try:
    import aiohttp
except Exception as exc:
    aiohttp = None
    _AIOHTTP_IMPORT_ERROR = exc
else:
    _AIOHTTP_IMPORT_ERROR = None


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "polymarket_weather_demo_output"
REPORT_TXT_PATH = OUTPUT_DIR / "demo_trade_report.txt"
TRADES_CSV_PATH = OUTPUT_DIR / "demo_trades.csv"
TRADES_JSONL_PATH = OUTPUT_DIR / "demo_trades.jsonl"
STATE_JSON_PATH = OUTPUT_DIR / "demo_state.json"

EVENT_REFRESH_SECONDS = 15 * 60
TRUTH_POLL_SECONDS = 5
REPORT_FLUSH_SECONDS = 5
LOOP_SLEEP_SECONDS = 1
ENTRY_NOTIONAL_USD = Decimal("10")
ENTRY_MAX_NO_ASK = Decimal("0.95")
EXIT_MIN_NO_BID = Decimal("0.99")
STOP_LOSS_BID: Optional[Decimal] = None
MIN_IMPOSSIBILITY_EDGE = Decimal("0")
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HTTP_TIMEOUT_SECONDS = 30
WS_RECONNECT_SECONDS = 5

MONTHS = {
    1: "january",
    2: "february",
    3: "march",
    4: "april",
    5: "may",
    6: "june",
    7: "july",
    8: "august",
    9: "september",
    10: "october",
    11: "november",
    12: "december",
}
MONTHS_INV = {v: k for k, v in MONTHS.items()}


def now_local_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def log(tag: str, text: str) -> None:
    print(f"[{now_local_str()}] [{tag}] {text}", flush=True)


def safe_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def must_decimal(value: Any, field_name: str) -> Decimal:
    out = safe_decimal(value)
    if out is None:
        raise RuntimeError(f"Invalid decimal for {field_name}: {value!r}")
    return out


def fmt_dec(value: Any, places: int = 4) -> str:
    if value is None:
        return ""
    dec_value = safe_decimal(value)
    if dec_value is None:
        return str(value)
    q = Decimal("1").scaleb(-places)
    return f"{dec_value.quantize(q):f}"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def slug_date_tokens() -> List[str]:
    today = datetime.now(UTC).date()
    tomorrow = today + date.resolution
    out: List[str] = []
    for d in (today, tomorrow):
        out.append(f"on-{MONTHS[d.month]}-{d.day}-{d.year}")
    return out


def is_target_event_slug(slug: str, tokens: Iterable[str]) -> bool:
    s = str(slug or "").lower()
    if not s.startswith("highest-temperature-"):
        return False
    return any(tok in s for tok in tokens)


def parse_event_date_from_slug(slug: str) -> date:
    m = re.search(r"on-([a-z]+)-(\d{1,2})-(\d{4})$", str(slug or "").lower())
    if not m:
        raise RuntimeError(f"Cannot parse date from slug: {slug}")
    month_num = MONTHS_INV[m.group(1)]
    return date(int(m.group(3)), month_num, int(m.group(2)))


def resolution_source_to_history_page(resolution_source: str, event_date: date) -> str:
    base = str(resolution_source or "").strip().rstrip("/")
    if not base.startswith("https://www.wunderground.com"):
        raise RuntimeError(f"Unsupported resolutionSource: {resolution_source!r}")
    base = re.sub(r"/date/\d{4}-\d{1,2}-\d{1,2}$", "", base)
    return f"{base}/date/{event_date.year}-{event_date.month}-{event_date.day}"


@dataclass
class BucketSpec:
    raw_title: str
    unit: str
    kind: str
    low: Optional[Decimal] = None
    high: Optional[Decimal] = None

    def impossible_side_for_running_max(self, current_max: Decimal) -> Optional[str]:
        if self.kind in {"exact", "range", "or_below"}:
            if self.high is not None and current_max > self.high:
                return "No"
            return None
        if self.kind == "or_higher":
            if self.low is not None and current_max >= self.low:
                return "Yes"
            return None
        return None


@dataclass
class MarketRef:
    event_id: str
    event_slug: str
    event_title: str
    page_url: str
    station_page_url: str
    city_key: str
    event_date_iso: str
    market_id: str
    market_slug: str
    question: str
    group_item_title: str
    bucket: BucketSpec
    yes_token_id: str
    no_token_id: str
    order_min_size: Decimal
    tick_size: Decimal
    best_bid_seed: Optional[Decimal]
    best_ask_seed: Optional[Decimal]
    last_trade_seed: Optional[Decimal]
    outcome_prices_seed: Dict[str, Decimal]


@dataclass
class Position:
    trade_id: str
    token_id: str
    outcome: str
    event_slug: str
    market_slug: str
    question: str
    group_item_title: str
    page_url: str
    city_key: str
    event_date_iso: str
    bucket_unit: str
    current_max_at_entry: Decimal
    entry_signal_epoch: int
    entry_time_local: str
    entry_price: Decimal
    qty: Decimal
    notional_usd: Decimal
    reason: str
    status: str = "OPEN"
    exit_price: Optional[Decimal] = None
    exit_time_local: str = ""
    exit_reason: str = ""
    realized_pnl: Optional[Decimal] = None
    max_bid_seen: Optional[Decimal] = None
    min_bid_seen: Optional[Decimal] = None


@dataclass
class OrderBookState:
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    last_trade: Optional[Decimal] = None
    last_update_local: str = ""
    last_event_type: str = ""
    is_ws_live: bool = False


@dataclass
class TruthSnapshot:
    page_url: str
    station_page_url: str
    event_date_iso: str
    unit: str
    latest_value: Decimal
    latest_time_text: str
    latest_valid_time_gmt: int
    running_max: Decimal
    row_count: int
    row_index: int
    source_mode: str
    obs_name: str
    api_url: str
    location_id: str
    geocode: str
    timezone_name: str
    timezone_offset_minutes: int


class WeatherMonitorAdapter:
    def __init__(self) -> None:
        module_path = BASE_DIR / "weather_monitor.py"
        if not module_path.exists():
            raise RuntimeError("weather_monitor.py not found in the same folder")
        spec = importlib.util.spec_from_file_location("weather_monitor_lib", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load weather_monitor.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(spec.name, None)
            raise
        self.m = module
        self.session = module.build_session()
        self.rate_limiter = module.RateLimiter(getattr(module, "MAX_REQUESTS_PER_SECOND", 6.0))
        self.discovery_cache = module.load_discovery_cache()
        self.station_info: Dict[str, Any] = {}

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _ensure_station_info(self, station_page_url: str) -> Any:
        info = self.station_info.get(station_page_url)
        if info is not None:
            return info
        spec = self.m.parse_station_spec_from_url(station_page_url)
        info = self.m.discover_or_load_station_info(self.session, spec, self.discovery_cache, self.rate_limiter)
        self.station_info[station_page_url] = info
        self.m.save_discovery_cache(self.discovery_cache)
        return info

    def _event_date_from_page(self, page_url: str) -> date:
        m = re.search(r"/date/(\d{4})-(\d{1,2})-(\d{1,2})$", page_url)
        if not m:
            raise RuntimeError(f"Cannot parse event date from page_url: {page_url}")
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def _station_page_from_page(self, page_url: str) -> str:
        return re.sub(r"/date/\d{4}-\d{1,2}-\d{1,2}$", "", page_url.strip().rstrip("/"))

    def _compact_date(self, event_date: date) -> str:
        return f"{event_date.year:04d}{event_date.month:02d}{event_date.day:02d}"

    def _iso_date(self, event_date: date) -> str:
        return f"{event_date.year:04d}-{event_date.month}-{event_date.day}"

    def _repair_location_id(self, station_page_url: str, page_url: str, info: Any) -> Any:
        candidate_urls = [
            f"{page_url}?unit=metric",
            f"{page_url}?unit=us",
            f"{station_page_url}?unit=metric",
            f"{station_page_url}?unit=us",
            station_page_url,
        ]
        old_location_id = str(info.location_id)
        new_location_id = old_location_id
        new_geocode = str(getattr(info, "geocode", "") or "")
        for url in candidate_urls:
            try:
                self.rate_limiter.wait_turn()
                response = self.session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
                response.raise_for_status()
                found_location_id, found_geocode = self.m.extract_page_details_from_text(response.text)
                if found_location_id:
                    new_location_id = found_location_id
                if found_geocode:
                    new_geocode = found_geocode
                if new_location_id and new_location_id != old_location_id:
                    break
            except Exception:
                continue
        if not new_location_id or new_location_id == old_location_id:
            return info
        timezone_name = str(getattr(info, "timezone_name", "") or "")
        timezone_offset_minutes = int(getattr(info, "timezone_offset_minutes", 0) or 0)
        if new_geocode:
            try:
                self.rate_limiter.wait_turn()
                tz_name, tz_offset = self.m.discover_station_timezone_info(self.session, new_geocode)
                if tz_name:
                    timezone_name = tz_name
                timezone_offset_minutes = int(tz_offset)
            except Exception:
                pass
        new_info = self.m.DiscoveryInfo(
            location_id=new_location_id,
            guessed_location_id=str(getattr(info, "guessed_location_id", "") or ""),
            geocode=new_geocode,
            timezone_name=timezone_name,
            timezone_offset_minutes=timezone_offset_minutes,
        )
        self.discovery_cache[station_page_url] = new_info
        self.station_info[station_page_url] = new_info
        self.m.save_discovery_cache(self.discovery_cache)
        log(
            "LOCATION_REPAIRED",
            f"station={station_page_url} old_location_id={old_location_id} new_location_id={new_location_id}",
        )
        return new_info

    def _fetch_payload(self, api_url: str) -> Dict[str, Any]:
        self.rate_limiter.wait_turn()
        response = self.session.get(api_url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()

    def fetch_latest_for_page(self, page_url: str) -> Dict[str, Dict[str, Any]]:
        station_page_url = self._station_page_from_page(page_url)
        event_date = self._event_date_from_page(page_url)
        event_date_iso = self._iso_date(event_date)
        compact_date = self._compact_date(event_date)
        info = self._ensure_station_info(station_page_url)

        station_tz = self.m.choose_station_timezone(
            str(getattr(info, "timezone_name", "") or ""),
            int(getattr(info, "timezone_offset_minutes", 0) or 0),
        )
        station_today = datetime.now(UTC).astimezone(station_tz).date()
        if event_date > station_today:
            raise RuntimeError(
                f"FUTURE_HISTORICAL_WAIT page={page_url} "
                f"event_date={event_date.isoformat()} station_today={station_today.isoformat()}"
            )

        out: Dict[str, Dict[str, Any]] = {}
        for unit in ("C", "F"):
            api_url = self.m.build_api_url(str(info.location_id), compact_date, unit)
            try:
                payload = self._fetch_payload(api_url)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 400:
                    info = self._repair_location_id(station_page_url, page_url, info)
                    api_url = self.m.build_api_url(str(info.location_id), compact_date, unit)
                    payload = self._fetch_payload(api_url)
                else:
                    raise

            observations = self.m.extract_observations_array(payload)
            if not observations:
                raise RuntimeError(f"No observations for page={page_url} unit={unit}")

            running_max: Optional[Decimal] = None
            for obs in observations:
                temp_val = self.m.extract_temp_from_observation(obs, unit)
                if running_max is None or temp_val > running_max:
                    running_max = temp_val
            if running_max is None:
                raise RuntimeError(f"No usable temps for page={page_url} unit={unit}")

            last = observations[-1]
            latest_value = self.m.extract_temp_from_observation(last, unit)
            valid_time_gmt_raw = (
                last.get("valid_time_gmt")
                if last.get("valid_time_gmt") is not None
                else last.get("expire_time_gmt")
            )
            if valid_time_gmt_raw is None:
                valid_time_gmt_raw = last.get("epoch")
            valid_time_gmt = int(valid_time_gmt_raw) if valid_time_gmt_raw is not None else 0
            obs_time_local = str(last.get("obsTimeLocal") or last.get("observationTime") or "")
            if not obs_time_local and valid_time_gmt:
                obs_time_local = datetime.fromtimestamp(valid_time_gmt, tz=timezone.utc).astimezone(station_tz).isoformat()
            latest_time_text = self.m.format_time_text_from_any(obs_time_local, valid_time_gmt, station_tz)
            obs_name = str(last.get("obsName") or last.get("stationID") or urlparse(station_page_url).path.rstrip("/").split("/")[-1])

            snapshot = TruthSnapshot(
                page_url=page_url,
                station_page_url=station_page_url,
                event_date_iso=event_date_iso,
                unit=unit,
                latest_value=latest_value,
                latest_time_text=latest_time_text,
                latest_valid_time_gmt=valid_time_gmt,
                running_max=running_max,
                row_count=len(observations),
                row_index=len(observations) - 1,
                source_mode="weather_monitor_api_historical_json",
                obs_name=obs_name,
                api_url=api_url,
                location_id=str(info.location_id),
                geocode=str(getattr(info, "geocode", "") or ""),
                timezone_name=str(getattr(info, "timezone_name", "") or ""),
                timezone_offset_minutes=int(getattr(info, "timezone_offset_minutes", 0) or 0),
            )
            out[unit] = asdict(snapshot)
        return out


class MarketBookWatcher:
    def __init__(self) -> None:
        self._desired: set[str] = set()
        self._lock = threading.Lock()
        self.books: Dict[str, OrderBookState] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._version = 0

    def set_desired_assets(self, token_ids: Iterable[str], seed_books: Dict[str, OrderBookState]) -> None:
        new_set = {str(x) for x in token_ids if str(x).strip()}
        with self._lock:
            if new_set == self._desired:
                for k, v in seed_books.items():
                    self.books.setdefault(k, v)
                return
            self._desired = new_set
            self._version += 1
            for k, v in seed_books.items():
                self.books.setdefault(k, v)
        log("WS_CFG", f"desired_assets={len(new_set)}")

    def get_book(self, token_id: str) -> OrderBookState:
        with self._lock:
            return self.books.get(token_id, OrderBookState())

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            out: Dict[str, Dict[str, str]] = {}
            for token_id, state in self.books.items():
                out[token_id] = {
                    "best_bid": fmt_dec(state.best_bid),
                    "best_ask": fmt_dec(state.best_ask),
                    "last_trade": fmt_dec(state.last_trade),
                    "last_update_local": state.last_update_local,
                    "last_event_type": state.last_event_type,
                    "is_ws_live": str(state.is_ws_live),
                }
            return out

    def start(self) -> None:
        if aiohttp is None:
            raise RuntimeError(f"aiohttp import failed: {_AIOHTTP_IMPORT_ERROR}")
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        log("WS", "watcher thread started")

    def stop(self) -> None:
        self._stop_event.set()

    def _thread_main(self) -> None:
        while not self._stop_event.is_set():
            try:
                asyncio.run(self._run_forever())
            except Exception as exc:
                log("WS_ERR", f"thread loop error: {type(exc).__name__}: {exc}")
                time.sleep(WS_RECONNECT_SECONDS)

    async def _run_forever(self) -> None:
        last_seen_version = -1
        while not self._stop_event.is_set():
            with self._lock:
                desired = sorted(self._desired)
                version = self._version
            if not desired:
                await asyncio.sleep(1)
                continue
            if version != last_seen_version:
                log("WS", f"connecting assets={len(desired)}")
                last_seen_version = version
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(WS_URL, heartbeat=20) as ws:
                        await ws.send_json({"assets_ids": desired, "type": "market"})
                        log("WS", f"subscribed assets={len(desired)}")
                        async for msg in ws:
                            if self._stop_event.is_set():
                                return
                            with self._lock:
                                changed = version != self._version
                            if changed:
                                log("WS", "desired asset set changed; reconnecting")
                                await ws.close()
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._handle_ws_payload(msg.data)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                log("WS_ERR", f"websocket error frame={msg.data}")
                                break
                            elif msg.type == aiohttp.WSMsgType.CLOSED:
                                log("WS", "websocket closed by remote")
                                break
            except Exception as exc:
                log("WS_ERR", f"connect/read failure: {type(exc).__name__}: {exc}")
            await asyncio.sleep(WS_RECONNECT_SECONDS)

    def _handle_ws_payload(self, raw_text: str) -> None:
        try:
            payload = json.loads(raw_text)
        except Exception:
            return
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "")
            if event_type == "book":
                self._handle_book(event)
            elif event_type == "price_change":
                self._handle_price_change(event)
            elif event_type == "last_trade_price":
                self._handle_last_trade(event)
            elif event_type == "best_bid_ask":
                self._handle_best_bid_ask(event)

    def _state_for(self, token_id: str) -> OrderBookState:
        return self.books.setdefault(token_id, OrderBookState())

    def _handle_book(self, event: Dict[str, Any]) -> None:
        token_id = str(event.get("asset_id") or "")
        if not token_id:
            return
        bids = event.get("bids", []) or []
        asks = event.get("asks", []) or []
        best_bid = None
        best_ask = None
        for bid in bids:
            px = safe_decimal((bid or {}).get("price"))
            if px is None:
                continue
            if best_bid is None or px > best_bid:
                best_bid = px
        for ask in asks:
            px = safe_decimal((ask or {}).get("price"))
            if px is None:
                continue
            if best_ask is None or px < best_ask:
                best_ask = px
        with self._lock:
            st = self._state_for(token_id)
            st.best_bid = best_bid
            st.best_ask = best_ask
            st.last_event_type = "book"
            st.last_update_local = now_local_str()
            st.is_ws_live = True

    def _handle_price_change(self, event: Dict[str, Any]) -> None:
        changes = event.get("price_changes")
        if not isinstance(changes, list):
            changes = event.get("changes")
        if not isinstance(changes, list):
            return
        with self._lock:
            for change in changes:
                if not isinstance(change, dict):
                    continue
                token_id = str(change.get("asset_id") or event.get("asset_id") or "")
                if not token_id:
                    continue
                st = self._state_for(token_id)
                best_bid = safe_decimal(change.get("best_bid"))
                best_ask = safe_decimal(change.get("best_ask"))
                if best_bid is not None:
                    st.best_bid = best_bid
                if best_ask is not None:
                    st.best_ask = best_ask
                st.last_event_type = "price_change"
                st.last_update_local = now_local_str()
                st.is_ws_live = True

    def _handle_last_trade(self, event: Dict[str, Any]) -> None:
        token_id = str(event.get("asset_id") or "")
        if not token_id:
            return
        with self._lock:
            st = self._state_for(token_id)
            price = safe_decimal(event.get("price"))
            if price is not None:
                st.last_trade = price
            st.last_event_type = "last_trade_price"
            st.last_update_local = now_local_str()
            st.is_ws_live = True

    def _handle_best_bid_ask(self, event: Dict[str, Any]) -> None:
        token_id = str(event.get("asset_id") or "")
        if not token_id:
            return
        with self._lock:
            st = self._state_for(token_id)
            best_bid = safe_decimal(event.get("best_bid"))
            best_ask = safe_decimal(event.get("best_ask"))
            if best_bid is not None:
                st.best_bid = best_bid
            if best_ask is not None:
                st.best_ask = best_ask
            st.last_event_type = "best_bid_ask"
            st.last_update_local = now_local_str()
            st.is_ws_live = True


class DemoTrader:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.truth = WeatherMonitorAdapter()
        self.watcher = MarketBookWatcher()
        self.active_markets: Dict[str, MarketRef] = {}
        self.page_to_markets: Dict[str, List[str]] = {}
        self.page_truth: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.page_max: Dict[Tuple[str, str], Decimal] = {}
        self.page_row_count: Dict[Tuple[str, str], int] = {}
        self.positions: Dict[str, Position] = {}
        self.trade_counter = 0
        self.last_event_refresh = 0.0
        self.last_truth_poll = 0.0
        self.last_report_flush = 0.0
        self.dirty_report = True
        self.last_truth_identity: Dict[Tuple[str, str], str] = {}
        self._entry_wait_ws_logged: set[str] = set()
        self._exit_wait_ws_logged: set[str] = set()

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
        self.truth.close()

    def run(self) -> int:
        self.watcher.start()
        log("START", "Polymarket weather demo trader started")
        try:
            while True:
                now_ts = time.time()
                if now_ts - self.last_event_refresh >= EVENT_REFRESH_SECONDS or not self.active_markets:
                    self.refresh_events_and_subscriptions()
                    self.last_event_refresh = now_ts
                if now_ts - self.last_truth_poll >= TRUTH_POLL_SECONDS:
                    self.poll_truth_once()
                    self.last_truth_poll = now_ts
                self.evaluate_entries_and_exits()
                if self.dirty_report and now_ts - self.last_report_flush >= REPORT_FLUSH_SECONDS:
                    self.flush_reports()
                    self.last_report_flush = now_ts
                time.sleep(LOOP_SLEEP_SECONDS)
        except KeyboardInterrupt:
            log("STOP", "Stopped by user")
            return 0
        finally:
            try:
                self.flush_reports()
            except Exception as exc:
                log("REPORT_ERR", f"flush on shutdown failed: {exc}")
            self.watcher.stop()
            self.close()

    def refresh_events_and_subscriptions(self) -> None:
        log("EVENTS", "refresh started")
        tokens = slug_date_tokens()
        events = self.fetch_events()
        filtered = []
        for event in events:
            if not is_target_event_slug(str(event.get("slug") or ""), tokens):
                continue
            resolution_source = str(event.get("resolutionSource") or "")
            if "https://www.wunderground.com" not in resolution_source:
                continue
            filtered.append(event)
        log("EVENTS", f"loaded={len(events)} filtered_weather={len(filtered)}")

        new_markets: Dict[str, MarketRef] = {}
        new_page_to_markets: Dict[str, List[str]] = {}
        seed_books: Dict[str, OrderBookState] = {}

        for event in filtered:
            try:
                event_slug = str(event.get("slug") or "")
                event_date = parse_event_date_from_slug(event_slug)
                page_url = resolution_source_to_history_page(str(event.get("resolutionSource") or ""), event_date)
                station_page_url = re.sub(r"/date/\d{4}-\d{1,2}-\d{1,2}$", "", page_url)
                city_key = self.city_key_from_slug(event_slug)
                event_id = str(event.get("id") or "")
                event_title = str(event.get("title") or "")
                for market in event.get("markets", []) or []:
                    if not market.get("active"):
                        continue
                    if not market.get("acceptingOrders", True):
                        continue
                    market_ref = self.market_from_gamma(
                        event_id=event_id,
                        event_slug=event_slug,
                        event_title=event_title,
                        city_key=city_key,
                        page_url=page_url,
                        station_page_url=station_page_url,
                        event_date_iso=event_date.isoformat(),
                        market=market,
                    )
                    if market_ref is None:
                        continue
                    new_markets[market_ref.no_token_id] = market_ref
                    new_page_to_markets.setdefault(page_url, []).append(market_ref.no_token_id)
                    seed_books[market_ref.no_token_id] = OrderBookState(
                        best_bid=None,
                        best_ask=None,
                        last_trade=None,
                        last_update_local="",
                        last_event_type="seed",
                        is_ws_live=False,
                    )
            except Exception as exc:
                log("EVENT_WARN", f"skip event slug={event.get('slug')} err={type(exc).__name__}: {exc}")

        self.active_markets = new_markets
        self.page_to_markets = new_page_to_markets
        self.watcher.set_desired_assets(new_markets.keys(), seed_books)
        log("EVENTS", f"tracked_event_days={len(self.page_to_markets)} tracked_no_tokens={len(self.active_markets)}")
        self.dirty_report = True

    def fetch_events(self) -> List[Dict[str, Any]]:
        url = "https://gamma-api.polymarket.com/events"
        offset = 0
        out: List[Dict[str, Any]] = []
        while True:
            resp = self.session.get(
                url,
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "offset": offset,
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        return out

    def city_key_from_slug(self, slug: str) -> str:
        m = re.match(r"highest-temperature-in-(.+)-on-[a-z]+-\d{1,2}-\d{4}$", slug)
        if not m:
            return slug
        return m.group(1)

    def market_from_gamma(
        self,
        event_id: str,
        event_slug: str,
        event_title: str,
        city_key: str,
        page_url: str,
        station_page_url: str,
        event_date_iso: str,
        market: Dict[str, Any],
    ) -> Optional[MarketRef]:
        outcomes = json.loads(str(market.get("outcomes") or "[]"))
        token_ids = json.loads(str(market.get("clobTokenIds") or "[]"))
        outcome_prices_raw = json.loads(str(market.get("outcomePrices") or "[]"))
        if len(outcomes) != 2 or len(token_ids) != 2:
            return None
        outcome_to_token = {str(outcomes[i]): str(token_ids[i]) for i in range(2)}
        if "No" not in outcome_to_token or "Yes" not in outcome_to_token:
            return None
        seed_prices: Dict[str, Decimal] = {}
        for idx, outcome in enumerate(outcomes):
            px = safe_decimal(outcome_prices_raw[idx] if idx < len(outcome_prices_raw) else None)
            if px is not None:
                seed_prices[str(outcome)] = px
        group_item_title = str(market.get("groupItemTitle") or market.get("question") or "")
        bucket = parse_bucket_spec(group_item_title)
        if bucket is None:
            return None
        return MarketRef(
            event_id=event_id,
            event_slug=event_slug,
            event_title=event_title,
            page_url=page_url,
            station_page_url=station_page_url,
            city_key=city_key,
            event_date_iso=event_date_iso,
            market_id=str(market.get("id") or ""),
            market_slug=str(market.get("slug") or ""),
            question=str(market.get("question") or ""),
            group_item_title=group_item_title,
            bucket=bucket,
            yes_token_id=outcome_to_token["Yes"],
            no_token_id=outcome_to_token["No"],
            order_min_size=must_decimal(market.get("orderMinSize", "5"), "orderMinSize"),
            tick_size=must_decimal(market.get("orderPriceMinTickSize", "0.001"), "tick_size"),
            best_bid_seed=safe_decimal(market.get("bestBid")),
            best_ask_seed=safe_decimal(market.get("bestAsk")),
            last_trade_seed=safe_decimal(market.get("lastTradePrice")),
            outcome_prices_seed=seed_prices,
        )

    def poll_truth_once(self) -> None:
        for page_url in sorted(self.page_to_markets.keys()):
            try:
                truth_map = self.truth.fetch_latest_for_page(page_url)
                self.page_truth[page_url] = truth_map
                for unit, payload in truth_map.items():
                    key = (page_url, unit)
                    identity = f"{payload['latest_valid_time_gmt']}|{fmt_dec(safe_decimal(payload['latest_value']), 6)}|{payload['row_count']}"
                    latest_val = must_decimal(payload["latest_value"], "latest_value")
                    running_max = must_decimal(payload["running_max"], "running_max")
                    prev_max = self.page_max.get(key)
                    prev_count = self.page_row_count.get(key, 0)
                    self.page_row_count[key] = int(payload["row_count"])
                    if self.last_truth_identity.get(key) != identity:
                        self.last_truth_identity[key] = identity
                        if prev_max is None or running_max > prev_max:
                            self.page_max[key] = running_max
                            log(
                                "HIGH_NEW",
                                f"page={page_url} unit={unit} latest={fmt_dec(latest_val, 3)} high_so_far={fmt_dec(running_max, 3)} row_count={payload['row_count']} valid_time={payload['latest_valid_time_gmt']} source={payload['source_mode']}",
                            )
                        else:
                            self.page_max[key] = prev_max
                            log(
                                "HIGH_STATE",
                                f"page={page_url} unit={unit} latest={fmt_dec(latest_val, 3)} high_so_far={fmt_dec(prev_max, 3)} row_count={payload['row_count']} valid_time={payload['latest_valid_time_gmt']} source={payload['source_mode']}",
                            )
                        self.dirty_report = True
                    elif int(payload["row_count"]) != prev_count:
                        if prev_max is None or running_max > prev_max:
                            self.page_max[key] = running_max
                        log(
                            "HIGH_STATE",
                            f"page={page_url} unit={unit} latest={fmt_dec(latest_val, 3)} high_so_far={fmt_dec(self.page_max.get(key), 3)} row_count={payload['row_count']} valid_time={payload['latest_valid_time_gmt']} source={payload['source_mode']}",
                        )
            except RuntimeError as exc:
                msg = str(exc)
                if msg.startswith("FUTURE_HISTORICAL_WAIT "):
                    log("TRUTH_WAIT", msg)
                    continue
                log("TRUTH_ERR", f"page={page_url} err={type(exc).__name__}: {exc}")
            except Exception as exc:
                log("TRUTH_ERR", f"page={page_url} err={type(exc).__name__}: {exc}")

    def evaluate_entries_and_exits(self) -> None:
        for token_id, market in sorted(self.active_markets.items()):
            book = self.watcher.get_book(token_id)
            impossible_side, current_max = self.current_impossibility(market)
            if impossible_side == "No":
                self.maybe_open_no_position(market, book, current_max)
            self.maybe_close_position(market, book, impossible_side)

    def current_impossibility(self, market: MarketRef) -> Tuple[Optional[str], Optional[Decimal]]:
        key = (market.page_url, market.bucket.unit)
        current_max = self.page_max.get(key)
        if current_max is None:
            return None, None
        side = market.bucket.impossible_side_for_running_max(current_max)
        return side, current_max

    def maybe_open_no_position(self, market: MarketRef, book: OrderBookState, current_max: Optional[Decimal]) -> None:
        if current_max is None:
            return
        if market.no_token_id in self.positions:
            return
        if not book.is_ws_live:
            if market.no_token_id not in self._entry_wait_ws_logged:
                self._entry_wait_ws_logged.add(market.no_token_id)
                log("ENTRY_WAIT_WS", f"token={market.no_token_id} question={market.question}")
            return
        self._entry_wait_ws_logged.discard(market.no_token_id)

        ask = book.best_ask
        if ask is None:
            log("ENTRY_WAIT_ASK", f"token={market.no_token_id} question={market.question}")
            return
        if ask > ENTRY_MAX_NO_ASK:
            log("ENTRY_SKIP", f"token={market.no_token_id} ask={fmt_dec(ask)} > max_entry={fmt_dec(ENTRY_MAX_NO_ASK)} question={market.question}")
            return
        fair_edge = Decimal("1") - ask
        if fair_edge < MIN_IMPOSSIBILITY_EDGE:
            return
        qty = ENTRY_NOTIONAL_USD / ask
        if qty < market.order_min_size:
            log("ENTRY_SKIP", f"token={market.no_token_id} qty={fmt_dec(qty)} < orderMinSize={fmt_dec(market.order_min_size)}")
            return
        self.trade_counter += 1
        trade_id = f"T{self.trade_counter:06d}"
        position = Position(
            trade_id=trade_id,
            token_id=market.no_token_id,
            outcome="No",
            event_slug=market.event_slug,
            market_slug=market.market_slug,
            question=market.question,
            group_item_title=market.group_item_title,
            page_url=market.page_url,
            city_key=market.city_key,
            event_date_iso=market.event_date_iso,
            bucket_unit=market.bucket.unit,
            current_max_at_entry=current_max,
            entry_signal_epoch=int(time.time()),
            entry_time_local=now_local_str(),
            entry_price=ask,
            qty=qty,
            notional_usd=ENTRY_NOTIONAL_USD,
            reason=f"running_max={fmt_dec(current_max, 3)} made YES impossible for bucket {market.group_item_title}",
            max_bid_seen=book.best_bid,
            min_bid_seen=book.best_bid,
        )
        self.positions[market.no_token_id] = position
        log("ENTRY", f"trade_id={trade_id} city={market.city_key} bucket='{market.group_item_title}' no_ask={fmt_dec(ask)} qty={fmt_dec(qty)} current_max={fmt_dec(current_max, 3)} page={market.page_url}")
        self.append_trade_jsonl(position)
        self.dirty_report = True

    def maybe_close_position(self, market: MarketRef, book: OrderBookState, impossible_side: Optional[str]) -> None:
        position = self.positions.get(market.no_token_id)
        if position is None or position.status != "OPEN":
            return
        if not book.is_ws_live:
            if market.no_token_id not in self._exit_wait_ws_logged:
                self._exit_wait_ws_logged.add(market.no_token_id)
                log("EXIT_WAIT_WS", f"trade_id={position.trade_id} token={market.no_token_id} question={market.question}")
            return
        self._exit_wait_ws_logged.discard(market.no_token_id)

        bid = book.best_bid
        if bid is None:
            log("EXIT_WAIT_BID", f"trade_id={position.trade_id} token={market.no_token_id} question={market.question}")
            return
        if position.max_bid_seen is None or bid > position.max_bid_seen:
            position.max_bid_seen = bid
        if position.min_bid_seen is None or bid < position.min_bid_seen:
            position.min_bid_seen = bid
        close_reason = ""
        if bid >= EXIT_MIN_NO_BID:
            close_reason = "target"
        elif STOP_LOSS_BID is not None and bid <= STOP_LOSS_BID:
            close_reason = "stop_loss"
        elif impossible_side not in (None, "No"):
            close_reason = "thesis_invalidated"
        if not close_reason:
            return
        proceeds = bid * position.qty
        cost = position.entry_price * position.qty
        position.exit_price = bid
        position.exit_time_local = now_local_str()
        position.exit_reason = close_reason
        position.realized_pnl = proceeds - cost
        position.status = "CLOSED"
        log("EXIT", f"trade_id={position.trade_id} reason={close_reason} bid={fmt_dec(bid)} pnl={fmt_dec(position.realized_pnl)} question={position.question}")
        self.append_trade_jsonl(position)
        self.dirty_report = True

    def append_trade_jsonl(self, position: Position) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with TRADES_JSONL_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(self.position_payload(position), ensure_ascii=False) + "\n")

    def position_payload(self, position: Position) -> Dict[str, Any]:
        payload = asdict(position)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = str(value)
        return payload

    def flush_reports(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.write_csv()
        self.write_summary_txt()
        self.write_state_json()
        self.last_report_flush = time.time()
        self.dirty_report = False
        log("REPORT", f"updated {REPORT_TXT_PATH}")

    def write_csv(self) -> None:
        rows = [self.position_payload(p) for p in sorted(self.positions.values(), key=lambda x: x.trade_id)]
        fieldnames = [
            "trade_id", "status", "token_id", "outcome", "event_slug", "market_slug", "question",
            "group_item_title", "page_url", "city_key", "event_date_iso", "bucket_unit", "current_max_at_entry",
            "entry_signal_epoch", "entry_time_local", "entry_price", "qty", "notional_usd", "reason",
            "exit_price", "exit_time_local", "exit_reason", "realized_pnl", "max_bid_seen", "min_bid_seen",
        ]
        tmp = TRADES_CSV_PATH.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        os.replace(tmp, TRADES_CSV_PATH)

    def write_summary_txt(self) -> None:
        open_positions = [p for p in self.positions.values() if p.status == "OPEN"]
        closed_positions = [p for p in self.positions.values() if p.status == "CLOSED"]
        total_realized = sum((p.realized_pnl or Decimal("0")) for p in closed_positions)
        mtm_open = Decimal("0")
        for p in open_positions:
            book = self.watcher.get_book(p.token_id)
            bid = book.best_bid
            if bid is None:
                continue
            mtm_open += (bid - p.entry_price) * p.qty

        lines: List[str] = []
        lines.append("Polymarket weather demo trader")
        lines.append(f"Updated: {now_local_str()}")
        lines.append("")
        lines.append("Configuration")
        lines.append(f"  ENTRY_NOTIONAL_USD = {ENTRY_NOTIONAL_USD}")
        lines.append(f"  ENTRY_MAX_NO_ASK  = {ENTRY_MAX_NO_ASK}")
        lines.append(f"  EXIT_MIN_NO_BID   = {EXIT_MIN_NO_BID}")
        lines.append(f"  STOP_LOSS_BID     = {STOP_LOSS_BID}")
        lines.append("")
        lines.append("Portfolio")
        lines.append(f"  tracked_event_days    = {len(self.page_to_markets)}")
        lines.append(f"  tracked_no_tokens     = {len(self.active_markets)}")
        lines.append(f"  open_positions        = {len(open_positions)}")
        lines.append(f"  closed_positions      = {len(closed_positions)}")
        lines.append(f"  realized_pnl_usd      = {fmt_dec(total_realized)}")
        lines.append(f"  open_mark_to_market   = {fmt_dec(mtm_open)}")
        lines.append("")
        lines.append("Running maxima")
        for (page_url, unit), value in sorted(self.page_max.items()):
            row_count = self.page_row_count.get((page_url, unit), 0)
            lines.append(f"  {unit} {fmt_dec(value, 3)} | rows={row_count} | {page_url}")
        lines.append("")
        lines.append("Open positions")
        if not open_positions:
            lines.append("  none")
        else:
            for p in sorted(open_positions, key=lambda x: x.trade_id):
                book = self.watcher.get_book(p.token_id)
                lines.append(
                    "  "
                    f"{p.trade_id} | {p.city_key} | {p.group_item_title} | entry={fmt_dec(p.entry_price)} | "
                    f"bid={fmt_dec(book.best_bid)} | ask={fmt_dec(book.best_ask)} | qty={fmt_dec(p.qty)} | reason={p.reason}"
                )
        lines.append("")
        lines.append("Recent closed positions")
        if not closed_positions:
            lines.append("  none")
        else:
            for p in sorted(closed_positions, key=lambda x: x.trade_id)[-20:]:
                lines.append(
                    "  "
                    f"{p.trade_id} | {p.city_key} | {p.group_item_title} | entry={fmt_dec(p.entry_price)} | "
                    f"exit={fmt_dec(p.exit_price)} | pnl={fmt_dec(p.realized_pnl)} | reason={p.exit_reason}"
                )
        atomic_write_text(REPORT_TXT_PATH, "\n".join(lines) + "\n")

    def write_state_json(self) -> None:
        payload = {
            "updated_local": now_local_str(),
            "active_market_count": len(self.active_markets),
            "page_to_markets": self.page_to_markets,
            "page_truth": self._jsonify_page_truth(),
            "page_max": {f"{page}|{unit}": str(value) for (page, unit), value in self.page_max.items()},
            "page_row_count": {f"{page}|{unit}": value for (page, unit), value in self.page_row_count.items()},
            "positions": [self.position_payload(p) for p in sorted(self.positions.values(), key=lambda x: x.trade_id)],
            "books": self.watcher.snapshot(),
        }
        atomic_write_json(STATE_JSON_PATH, payload)

    def _jsonify_page_truth(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for page_url, unit_map in self.page_truth.items():
            out[page_url] = {}
            for unit, payload in unit_map.items():
                item: Dict[str, Any] = {}
                for key, value in payload.items():
                    if isinstance(value, Decimal):
                        item[key] = str(value)
                    else:
                        item[key] = value
                out[page_url][unit] = item
        return out


def parse_bucket_spec(text: str) -> Optional[BucketSpec]:
    s = str(text or "").strip()
    if not s:
        return None

    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)[°º]\s*([CF])", s, flags=re.IGNORECASE)
    if m:
        return BucketSpec(raw_title=s, unit=m.group(3).upper(), kind="range", low=Decimal(m.group(1)), high=Decimal(m.group(2)))

    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)[°º]\s*([CF])\s+or\s+below", s, flags=re.IGNORECASE)
    if m:
        val = Decimal(m.group(1))
        return BucketSpec(raw_title=s, unit=m.group(2).upper(), kind="or_below", low=None, high=val)

    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)[°º]\s*([CF])\s+or\s+higher", s, flags=re.IGNORECASE)
    if m:
        val = Decimal(m.group(1))
        return BucketSpec(raw_title=s, unit=m.group(2).upper(), kind="or_higher", low=val, high=None)

    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)[°º]\s*([CF])", s, flags=re.IGNORECASE)
    if m:
        val = Decimal(m.group(1))
        return BucketSpec(raw_title=s, unit=m.group(2).upper(), kind="exact", low=val, high=val)

    return None


def main() -> int:
    trader = DemoTrader()
    return trader.run()


if __name__ == "__main__":
    try:
        rc = main()
        print()
        input("Press Enter to exit...")
        raise SystemExit(rc)
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc()
        print(f"[FATAL] {type(exc).__name__}: {exc}", flush=True)
        print()
        input("Press Enter to exit...")
        raise
