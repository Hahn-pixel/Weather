#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable, Tuple

from config import MATCH_TOLERANCE_C, MATCH_TOLERANCE_F
from helpers import decimal_key, epoch_to_utc_str, fmt_decimal
from models import ApiBundle, MonitorMemory, PendingApiValue, UiState


def stream_tolerance(stream_unit: str) -> Decimal:
    return MATCH_TOLERANCE_C if stream_unit == "C" else MATCH_TOLERANCE_F


def api_stream_value(bundle: ApiBundle, stream_unit: str) -> Decimal:
    return bundle.value_c if stream_unit == "C" else bundle.value_f


def api_stream_meta(bundle: ApiBundle, stream_unit: str) -> Tuple[str, int]:
    if stream_unit == "C":
        return bundle.obs_time_local_c, bundle.valid_time_gmt_c
    return bundle.obs_time_local_f, bundle.valid_time_gmt_f


def ui_identity(state: UiState) -> str:
    return f"{state.stream_unit}|{state.source_mode}|{fmt_decimal(state.value_raw, 6)}|{state.chart_index}|{state.callout_text}"


def process_api_state(memory: MonitorMemory, bundle: ApiBundle, log: Callable) -> None:
    identity = (
        f"c:{bundle.valid_time_gmt_c}|{fmt_decimal(bundle.value_c, 6)}|{bundle.obs_time_local_c}"
        f"|f:{bundle.valid_time_gmt_f}|{fmt_decimal(bundle.value_f, 6)}|{bundle.obs_time_local_f}"
    )

    if identity == memory.last_api_identity:
        return

    memory.last_api_identity = identity
    now_epoch = time.time()

    for stream_unit in ("C", "F"):
        stream_value = api_stream_value(bundle, stream_unit)
        obs_time_local, valid_time_gmt = api_stream_meta(bundle, stream_unit)
        key = decimal_key(stream_value)
        bucket = memory.pending_by_stream_key[stream_unit]
        existing = bucket.get(key)

        if existing is None:
            bucket[key] = PendingApiValue(
                stream_unit=stream_unit,
                stream_value=stream_value,
                first_seen_epoch=now_epoch,
                first_seen_wall=time.strftime("%Y-%m-%d %H:%M:%S"),
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


def sweep_unmatched_api(memory: MonitorMemory, newest_api_bundle: ApiBundle, log: Callable) -> None:
    for stream_unit in ("C", "F"):
        newest_stream_value = api_stream_value(newest_api_bundle, stream_unit)
        newest_key = decimal_key(newest_stream_value)
        bucket = memory.pending_by_stream_key[stream_unit]
        to_remove = []

        for key, pending in bucket.items():
            if pending.matched:
                continue
            if key != newest_key:
                log(
                    "API_REPLACED_BEFORE_UI",
                    (
                        f"stream={stream_unit} pending_value={fmt_decimal(pending.stream_value)}°{stream_unit} "
                        f"api_obs_time_local={pending.obs_time_local} replaced_by_newer_api_before_ui_match=1"
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


def evaluate_matches(memory: MonitorMemory, ui_state: UiState, log: Callable) -> None:
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
                f"stream={stream_unit} value={fmt_decimal(ui_value)}°{stream_unit} lag_seconds={lag_seconds:.1f} "
                f"api_first_seen={pending.first_seen_wall} api_obs_time_local={pending.obs_time_local}"
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
        state_label = "WITHIN_TOLERANCE" if delta <= stream_tolerance(stream_unit) else "WAITING_UI_OR_MISMATCH"

        log(
            "DIFF",
            (
                f"stream={stream_unit} ui_value={fmt_decimal(ui_value)}°{stream_unit} "
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


def process_ui_state(memory: MonitorMemory, state: UiState, log: Callable) -> None:
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
            f"stream={state.stream_unit} source_mode={state.source_mode} value={fmt_decimal(state.value_raw)}°{state.stream_unit} "
            f"legend={state.legend_text!r} callout={state.callout_text!r} chart_index={state.chart_index} bar_count={state.bar_count}{extra}"
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

    evaluate_matches(memory, state, log)