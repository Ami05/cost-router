# Baseline Collection for `prompt_text_empty` Alert

## Overview

The `prompt_text_empty` alert must not have thresholds set without an established baseline. This document guides the 7-day silent collection period and threshold calibration.

## Silent Collection Period (7 days)

1. Deploy the alert with **no thresholds** (all gates disabled or set to `null`).
2. Let the system run for **7 consecutive days** of production traffic.
3. Collect all `prompt_text_empty` daily rate observations into `routing_events.jsonl` or equivalent telemetry sink.

## Baseline Computation

After 7 days, compute percentiles over **daily rates** (not raw events):

| Percentile | Computation | Purpose |
|---|---|---|
| **p50** | Median daily rate | Normal / typical behavior |
| **p95** | 95th percentile daily rate | Elevated but acceptable |
| **p99** | 99th percentile daily rate | Near-maximum observed |

**Formula:** For each day, sum all `prompt_text_empty` events and divide by total events that day to get the daily rate. Sort 7 daily rates, extract p50, p95, p99.

## Baseline Values Table

Fill in observed values from your 7-day run:

| Metric | Value | Notes |
|---|---|---|
| **p50 daily rate** | `___` | Typical day |
| **p95 daily rate** | `___` | Elevated day (95% of days below this) |
| **p99 daily rate** | `___` | Near-max day (99% of days below this) |
| **p99 * 1.5** | `___` | Recommended soft threshold (alert trigger) |

## Threshold Recommendation

- **Soft threshold (alert):** Set to `p99 * 1.5` — allows for natural variance above p99 but flags true anomalies.
- **Hard threshold (block/action):** Set to `p99 * 2.0` or higher — only trigger intervention for extreme outliers.
- **Silent threshold (collection-only):** `null` or `0` — during this period, all events are logged but no alerts fire.

## Implementation

Once thresholds are calibrated:

1. Update `config.json` (or alert definition) with `soft_threshold` and `hard_threshold`.
2. Deploy and monitor for regression — if alert fires consistently, increase thresholds; if silent too often, lower them.
3. Document the baseline values and threshold dates in your incident runbook or wiki.

## Notes

- **Data leakage:** Ensure the 7-day baseline period includes diverse traffic patterns (e.g., weekday + weekend, peak hours, low-traffic windows) for robust percentiles.
- **Rebaseline:** If routing logic or model changes significantly, rerun the 7-day collection to recalibrate.
- **Alert fatigue:** Thresholds set too low will fire constantly; thresholds too high will miss real issues. p99 * 1.5 is a reasonable starting point.
