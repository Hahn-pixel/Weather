#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import re
from typing import Dict

import requests

from config import API_KEY, REFERER, REQUEST_TIMEOUT_SECONDS, USER_AGENT, VERIFY_TLS
from helpers import derive_location_id_from_url, parse_decimal
from models import ApiBundle, ApiReading


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


def resolve_location_id_from_page_api(session: requests.Session, page_url: str) -> str:
    response = session.get(
        page_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
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


def fetch_historical_json(session: requests.Session, location_id: str, units: str, date_key: str) -> Dict:
    response = session.get(
        historical_url(location_id),
        params={
            "apiKey": API_KEY,
            "units": units,
            "startDate": date_key,
            "endDate": date_key,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_TLS,
    )
    response.raise_for_status()
    return response.json()


def extract_api_reading(data: Dict, units: str) -> ApiReading:
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


def fetch_api_bundle(session: requests.Session, location_id: str, date_key: str) -> ApiBundle:
    data_c = fetch_historical_json(session, location_id, "m", date_key)
    data_f = fetch_historical_json(session, location_id, "e", date_key)

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
        f"c:{bundle.valid_time_gmt_c}|{bundle.value_c}|{bundle.obs_time_local_c}"
        f"|f:{bundle.valid_time_gmt_f}|{bundle.value_f}|{bundle.obs_time_local_f}"
    )