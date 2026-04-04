#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

from config import (
    CHART_ACTIVATION_ATTEMPTS,
    CHART_READY_RETRY_DELAY_SECONDS,
    CHART_WAIT_TIMEOUT_SECONDS,
    HOVER_PAUSE_SECONDS,
    PAGE_LOAD_TIMEOUT_SECONDS,
    SHOW_BROWSER_WINDOW,
    TARGET_PAGE_URL,
    USER_AGENT,
)
from helpers import (
    infer_stream_unit_from_legend,
    parse_callout_value,
    parse_local_ticks,
    parse_path_points,
)
from models import UiState


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
            """
            try {
                const legends = Array.from(document.querySelectorAll('div.legend, .legend .legend-def'));
                return legends.some(x => /Temperature/i.test(x.textContent || ''));
            } catch (err) {
                window.__dbg_wait_error = String(err);
                return false;
            }
            """
        )
    )
    time.sleep(0.35)


def activate_temperature_chart_callouts(driver: webdriver.Chrome) -> None:
    script = r"""
    const attempts = arguments[0];
    const pauseMs = arguments[1];
    const callback = arguments[arguments.length - 1];
    const result = {attempts: attempts, charts: [], error: null};

    try {
      const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
      const normalizeText = (value) => (value || '').replace(/\s+/g, ' ').trim();

      const getLegendText = (container) => {
        try {
          return Array.from(container.querySelectorAll('.legend .legend-def'))
            .map(x => normalizeText(x.textContent || ''))
            .filter(Boolean)
            .join(' | ');
        } catch (err) {
          return '';
        }
      };

      const getCalloutText = (container) => {
        try {
          const el = container.querySelector('.callout.temperature .callout-text');
          return el ? normalizeText(el.textContent || '') : '';
        } catch (err) {
          return '';
        }
      };

      const getPath = (container) => {
        try {
          return container.querySelector('g.plot.temperature.line path');
        } catch (err) {
          return null;
        }
      };

      const dispatchAt = (target, x, y) => {
        const eventInit = {clientX: x, clientY: y, bubbles: true, cancelable: true, composed: true, view: window};
        ['pointerover', 'mouseover', 'pointerenter', 'mouseenter', 'pointermove', 'mousemove'].forEach(type => {
          const Ctor = type.startsWith('pointer') ? PointerEvent : MouseEvent;
          target.dispatchEvent(new Ctor(type, eventInit));
        });
      };

      const activateContainer = async (container, idx) => {
        const item = {
          idx: idx,
          legend: getLegendText(container),
          status: 'init',
          callout_before: getCalloutText(container),
          callout_after: '',
          path_present_before: false,
          path_present_after: false,
          bar_count: 0,
          attempts_used: 0
        };

        let bars = [];
        try {
          bars = Array.from(container.querySelectorAll('rect.bc-bar'));
          item.bar_count = bars.length;
        } catch (err) {
          bars = [];
        }

        let activated = false;

        for (let attempt = 1; attempt <= attempts; attempt += 1) {
          item.attempts_used = attempt;
          const path = getPath(container);
          item.path_present_before = item.path_present_before || !!path;

          if (path) {
            const rect = path.getBoundingClientRect();
            const x = Math.max(rect.left + 1, rect.right - 2);
            const y = rect.top + (rect.height / 2.0);
            dispatchAt(path, x, y);
            activated = true;
          } else if (bars.length) {
            const target = bars.length >= 2 ? bars[bars.length - 2] : bars[bars.length - 1];
            const rect = target.getBoundingClientRect();
            const x = rect.left + Math.max(1, rect.width - 2);
            const y = rect.top + Math.max(1, rect.height / 2.0);
            dispatchAt(target, x, y);
            activated = true;
          }

          await sleep(pauseMs);

          const callout = getCalloutText(container);
          if (callout) {
            item.callout_after = callout;
            item.path_present_after = !!getPath(container);
            item.status = activated ? 'ok' : 'no_target';
            return item;
          }
        }

        item.callout_after = getCalloutText(container);
        item.path_present_after = !!getPath(container);

        if (!activated) {
          item.status = bars.length ? 'bars_only_no_activation' : 'no_path_no_bars';
        } else if (item.callout_after) {
          item.status = 'late_callout';
        } else {
          item.status = 'activated_no_callout';
        }

        return item;
      };

      (async () => {
        const chartDivs = Array.from(document.querySelectorAll('lib-wu-chart .charts-canvas > div'));
        const temperatureContainers = chartDivs.filter(container => /Temperature/i.test(getLegendText(container)));
        for (let idx = 0; idx < temperatureContainers.length; idx += 1) {
          const item = await activateContainer(temperatureContainers[idx], idx);
          result.charts.push(item);
        }
        window.__dbg_hover_debug = result;
        callback(result);
      })().catch(err => {
        result.error = String(err);
        window.__dbg_hover_debug = result;
        callback(result);
      });
    } catch (err) {
      result.error = String(err);
      window.__dbg_hover_debug = result;
      callback(result);
    }
    """

    result = driver.execute_async_script(
        script,
        int(CHART_ACTIVATION_ATTEMPTS),
        int(max(1, round(HOVER_PAUSE_SECONDS * 1000))),
    )

    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(f"Chart activation JS failed: {result.get('error')}")


def wait_for_temperature_svg_paths(driver: webdriver.Chrome) -> None:
    deadline = time.time() + CHART_WAIT_TIMEOUT_SECONDS

    while time.time() < deadline:
        payload = driver.execute_script(
            """
            try {
              const chartDivs = Array.from(document.querySelectorAll('lib-wu-chart .charts-canvas > div'));
              const items = chartDivs.map(container => {
                const legendText = Array.from(container.querySelectorAll('.legend .legend-def'))
                  .map(x => (x.textContent || '').replace(/\\s+/g, ' ').trim())
                  .filter(Boolean)
                  .join(' | ');

                if (!/Temperature/i.test(legendText)) return null;

                const unit = /°C|\\(C\\)/i.test(legendText) ? 'C' : (/°F|\\(F\\)/i.test(legendText) ? 'F' : '?');

                return {
                  unit: unit,
                  pathPresent: !!container.querySelector('g.plot.temperature.line path'),
                  calloutPresent: !!container.querySelector('.callout.temperature .callout-text')
                };
              }).filter(Boolean);

              window.__dbg_path_wait = items;
              return items;
            } catch (err) {
              window.__dbg_path_wait_error = String(err);
              return [];
            }
            """
        )

        seen = {item.get("unit"): item for item in payload if isinstance(item, dict)}

        if (
            seen.get("C") and seen.get("F")
            and (seen["C"].get("pathPresent") or seen["C"].get("calloutPresent"))
            and (seen["F"].get("pathPresent") or seen["F"].get("calloutPresent"))
        ):
            return

        time.sleep(CHART_READY_RETRY_DELAY_SECONDS)

    raise RuntimeError("Temperature charts did not expose both C and F path/callout states before timeout.")


def extract_temperature_chart_payload(driver: webdriver.Chrome) -> Dict[str, Any]:
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
            yTicks.push({
              text: norm,
              x: String(rect.left - containerRect.left),
              y: String(rect.top - containerRect.top + rect.height / 2.0)
            });
          });

          const tempPath = container.querySelector('g.plot.temperature.line path');

          if (!tempPath) {
            result.charts.push({
              chartIndex: idx,
              legendText: temperatureLegend,
              calloutText: calloutText,
              yTicks: yTicks,
              pathD: '',
              pathPresent: false,
              barCount: Array.from(container.querySelectorAll('rect.bc-bar')).length,
              innerError: null
            });
            return;
          }

          const pathD = tempPath.getAttribute('d') || '';
          const bars = Array.from(container.querySelectorAll('rect.bc-bar'));

          result.charts.push({
            chartIndex: idx,
            legendText: temperatureLegend,
            calloutText: calloutText,
            yTicks: yTicks,
            pathD: pathD,
            pathPresent: true,
            barCount: bars.length,
            innerError: null
          });
        } catch (errInner) {
          result.charts.push({
            chartIndex: idx,
            legendText: '',
            calloutText: '',
            yTicks: [],
            pathD: '',
            pathPresent: false,
            barCount: 0,
            innerError: String(errInner)
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
        raise RuntimeError("Unexpected JS payload for temperature chart extraction.")

    if payload.get("jsError"):
        raise RuntimeError(f"Temperature chart JS extraction failed: {payload.get('jsError')}")

    return payload


def build_ui_state_from_chart(chart: Dict[str, Any], page_title: str) -> UiState:
    legend_text = str(chart.get("legendText") or "").strip()
    stream_unit = infer_stream_unit_from_legend(legend_text)

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
        path_points = parse_path_points(chart.get("pathD") or "")
        ticks = parse_local_ticks(chart.get("yTicks") or [])

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
    activate_temperature_chart_callouts(driver)
    wait_for_temperature_svg_paths(driver)

    payload = extract_temperature_chart_payload(driver)

    if payload.get("noData") and not payload.get("charts"):
        raise RuntimeError("History UI shows 'No Data Recorded'.")

    page_title = str(payload.get("pageTitle") or "")
    states: Dict[str, UiState] = {}
    chart_errors: List[str] = []

    for chart in payload.get("charts") or []:
        try:
            state = build_ui_state_from_chart(chart, page_title)
        except Exception as exc:
            chart_errors.append(
                f"idx={chart.get('chartIndex')} "
                f"legend={chart.get('legendText')!r} "
                f"path_present={chart.get('pathPresent')} "
                f"bar_count={chart.get('barCount')} "
                f"callout={chart.get('calloutText')!r} "
                f"err={exc}"
            )
            continue
        states[state.stream_unit] = state

    if not states:
        raise RuntimeError(f"No usable temperature charts found. Chart errors: {' | '.join(chart_errors)}")

    return states


def collect_debug_snapshot(driver: Optional[webdriver.Chrome]) -> Dict[str, Any]:
    if driver is None:
        return {}

    try:
        return driver.execute_script(
            """
            return {
              title: document.title || '',
              lastPayload: window.__dbg_last_payload || null,
              hoverDebug: window.__dbg_hover_debug || null,
              pathWait: window.__dbg_path_wait || null,
              callouts: Array.from(document.querySelectorAll('.callout.temperature .callout-text')).map(x => (x.textContent || '').trim()),
              legends: Array.from(document.querySelectorAll('.legend .legend-def')).map(x => (x.textContent || '').replace(/\\s+/g, ' ').trim())
            };
            """
        )
    except Exception:
        return {}