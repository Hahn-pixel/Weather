#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional


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