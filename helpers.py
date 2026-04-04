#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple


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


def parse_path_points(path_d: str) -> List[Tuple[Decimal, Decimal]]:
    tokens = re.findall(r"[ML]\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)", str(path_d or ""))
    if not tokens:
        raise RuntimeError("Temperature path d=... contains no points.")
    return [(parse_decimal(x, "path.x"), parse_decimal(y, "path.y")) for x, y in tokens]


def parse_local_ticks(ticks: List[Dict[str, Any]]) -> List[Tuple[Decimal, Decimal]]:
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


def infer_stream_unit_from_legend(legend_text: str) -> Optional[str]:
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