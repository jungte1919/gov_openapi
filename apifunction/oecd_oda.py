from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests

# OECD SDMX API (same underlying data as OECD Data Explorer DAC1)
# Dataflow: DAC1 — Flows by provider (ODA+OOF+Private)
DEFAULT_DATAFLOW_VERSION = "1.7"
SDMX_DATA_BASE = (
    "https://sdmx.oecd.org/public/rest/data/"
    f"OECD.DCD.FSD,DSD_DAC1@DF_DAC1,{DEFAULT_DATAFLOW_VERSION}"
)

# DAC measure / flow defaults aligned with Explorer presets for headline ODA
DEFAULT_MEASURE = "1010"  # Official Development Assistance (ODA)
DEFAULT_FLOW_TYPE = "1140"  # Disbursements, net
DEFAULT_SECTOR = "_Z"
DEFAULT_TYING = "_Z"
DEFAULT_UNIT = "USD"
DEFAULT_PRICE_BASE = "Q"  # constant prices (USD millions in DAC convention)

# Spacing between any two OECD SDMX downloads (DAC1 multi-donor = many requests).
_OECD_LAST_CALL_MONO: float = 0.0
# Space OECD calls to reduce HTTP 429s when pulling DAC1 per donor for several countries.
_OECD_MIN_INTERVAL_SEC = 18.0


def _oecd_wait_slot() -> None:
    global _OECD_LAST_CALL_MONO
    gap = time.monotonic() - _OECD_LAST_CALL_MONO
    if gap < _OECD_MIN_INTERVAL_SEC:
        time.sleep(_OECD_MIN_INTERVAL_SEC - gap)


def _oecd_mark_called() -> None:
    global _OECD_LAST_CALL_MONO
    _OECD_LAST_CALL_MONO = time.monotonic()


def _fetch_sdmx_json(
    url: str,
    params: dict[str, str],
    *,
    timeout: int,
    max_attempts: int = 8,
) -> dict[str, Any]:
    """GET with backoff on HTTP 429 (OECD rate limits burst per-donor fetches)."""
    delay = 5.0
    last_err: Optional[BaseException] = None
    for attempt in range(max_attempts):
        _oecd_wait_slot()
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code == 429:
            last_err = requests.HTTPError(
                f"429 Too Many Requests (attempt {attempt + 1}/{max_attempts})",
                response=resp,
            )
            time.sleep(min(120.0, delay))
            delay *= 1.65
            continue
        resp.raise_for_status()
        _oecd_mark_called()
        return resp.json()
    assert last_err is not None
    raise last_err


def _build_series_key_parts(dimensions: list[dict[str, Any]], key: str) -> dict[str, str]:
    parts = [int(p) for p in key.split(":")]
    out: dict[str, str] = {}
    for dim, idx in zip(dimensions, parts):
        dim_id = dim.get("id", "")
        vals = dim.get("values") or []
        if 0 <= idx < len(vals):
            out[dim_id] = str(vals[idx].get("id", ""))
        else:
            out[dim_id] = ""
    return out


def _parse_sdmx_json_dataset(payload: dict[str, Any]) -> pd.DataFrame:
    structures = payload.get("data", {}).get("structures", [])
    data_sets = payload.get("data", {}).get("dataSets", [])
    if not structures or not data_sets:
        return pd.DataFrame()
    struct0 = structures[0]
    series_dims_raw = struct0.get("dimensions", {}).get("series", [])
    obs_dims = struct0.get("dimensions", {}).get("observation", [])
    if not isinstance(series_dims_raw, list) or not series_dims_raw:
        return pd.DataFrame()

    series_dims = sorted(series_dims_raw, key=lambda d: int(d.get("keyPosition", 0)))
    time_dim = next((d for d in obs_dims if d.get("id") == "TIME_PERIOD"), None)
    if time_dim is None:
        return pd.DataFrame()
    time_values = time_dim.get("values") or []

    rows: list[dict[str, Any]] = []
    series_block = data_sets[0].get("series") or {}
    for skey, sdat in series_block.items():
        try:
            parts_map = _build_series_key_parts(series_dims, skey)
        except (ValueError, TypeError):
            continue
        observations = sdat.get("observations") or {}
        for tidx, obs in observations.items():
            try:
                ti = int(tidx)
            except (TypeError, ValueError):
                continue
            if ti < 0 or ti >= len(time_values):
                continue
            period = str(time_values[ti].get("id", "")).strip()
            if not period:
                continue
            val = None
            if isinstance(obs, (list, tuple)) and obs:
                val = obs[0]
            if val is None:
                continue
            try:
                val_f = float(val)
            except (TypeError, ValueError):
                continue
            row = {
                "period": period,
                "value": val_f,
                **{k.lower(): v for k, v in parts_map.items()},
            }
            # donor display name
            donor_idx = next(
                (i for i, d in enumerate(series_dims) if d.get("id") == "DONOR"), None
            )
            if donor_idx is not None:
                try:
                    di = int(skey.split(":")[donor_idx])
                    dv = series_dims[donor_idx]["values"][di]
                    row["donor_name"] = (dv.get("name") or dv.get("id") or "").strip()
                except (ValueError, IndexError, TypeError):
                    row["donor_name"] = row.get("donor", "")
            rows.append(row)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_records(rows)


def fetch_oecd_dac1_provider_flows(
    *,
    donors: Optional[list[str]] = None,
    start_period: int = 2000,
    end_period: Optional[int] = None,
    measure: str = DEFAULT_MEASURE,
    flow_type: str = DEFAULT_FLOW_TYPE,
    sector: str = DEFAULT_SECTOR,
    tying_status: str = DEFAULT_TYING,
    unit_measure: str = DEFAULT_UNIT,
    price_base: str = DEFAULT_PRICE_BASE,
    timeout: int = 120,
    cache_path: Optional[str | Path] = None,
    refresh: bool = True,
) -> pd.DataFrame:
    """
    Download OECD DAC Table DAC1 (flows by provider) via the public SDMX-JSON API.

    This is the same DAC1 series exposed in the OECD Data Explorer under
    development finance / ODA statistics. Default slice: headline ODA (measure
    1010), net disbursements (1140), USD, constant prices (Q).

    Returns a long DataFrame with at least: period, donor, donor_name, value,
    measure, flow_type, unit_measure, price_base, sector, tying_status.

    If ``cache_path`` is set and ``refresh`` is False, the JSON on disk is parsed
    without a network call (useful when the OECD endpoint is temporarily unavailable).

    When more than one donor is requested, this function runs **one SDMX request per
    donor** and concatenates the results. A single request with ``DONOR1+DONOR2+...`` in
    the key often returns an incomplete year range for some donors (notably Korea),
    which would bias ratios such as ODA/GNI.
    """
    if end_period is None:
        end_period = datetime.now().year
    donor_list = donors or ["KOR", "JPN", "USA", "DEU", "FRA", "GBR", "CAN", "AUS"]
    donor_list = [d.strip().upper() for d in donor_list if d and str(d).strip()]
    if not donor_list:
        return pd.DataFrame()

    cache_file = Path(cache_path) if cache_path else None

    if len(donor_list) > 1:
        per_donor_caches: list[Optional[Path]]
        if cache_file:
            stem, suffix = cache_file.stem, cache_file.suffix
            parent = cache_file.parent
            per_donor_caches = [parent / f"{stem}__{code}{suffix}" for code in donor_list]
        else:
            per_donor_caches = [None] * len(donor_list)

        if (not refresh) and cache_file and all(p and p.exists() for p in per_donor_caches):
            frames: list[pd.DataFrame] = []
            for p in per_donor_caches:
                raw = p.read_text(encoding="utf-8")
                frames.append(_parse_sdmx_json_dataset(json.loads(raw)))
            df_m = pd.concat(frames, ignore_index=True)
            if df_m.empty:
                return df_m
            df_m = df_m.rename(columns={"donor": "donor_id"})
            df_m["year"] = pd.to_numeric(df_m["period"], errors="coerce").astype("Int64")
            return df_m.sort_values(["donor_id", "year"]).reset_index(drop=True)

        frames = []
        for code, sub_cache in zip(donor_list, per_donor_caches):
            frames.append(
                fetch_oecd_dac1_provider_flows(
                    donors=[code],
                    start_period=start_period,
                    end_period=end_period,
                    measure=measure,
                    flow_type=flow_type,
                    sector=sector,
                    tying_status=tying_status,
                    unit_measure=unit_measure,
                    price_base=price_base,
                    timeout=timeout,
                    cache_path=sub_cache,
                    refresh=refresh,
                )
            )
        return pd.concat(frames, ignore_index=True).sort_values(
            ["donor_id", "year"]
        ).reset_index(drop=True)

    use_cached = bool(cache_file and cache_file.exists() and not refresh)

    key = ".".join(
        [
            "+".join(donor_list),
            sector,
            measure,
            tying_status,
            flow_type,
            unit_measure,
            price_base,
        ]
    )
    url = f"{SDMX_DATA_BASE}/{key}"
    params = {
        "startPeriod": str(start_period),
        "endPeriod": str(end_period),
        "format": "jsondata",
    }

    if use_cached:
        raw = cache_file.read_text(encoding="utf-8")  # type: ignore[union-attr]
        payload = json.loads(raw)
    else:
        payload = _fetch_sdmx_json(url, params, timeout=timeout)
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    df = _parse_sdmx_json_dataset(payload)
    if df.empty:
        return df
    df = df.rename(columns={"donor": "donor_id"})

    # Normalise period to int year when possible
    df["year"] = pd.to_numeric(df["period"], errors="coerce").astype("Int64")
    df = df.sort_values(["donor_id", "year"]).reset_index(drop=True)
    return df
