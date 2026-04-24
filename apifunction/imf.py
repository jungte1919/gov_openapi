from __future__ import annotations

from typing import Any, Mapping, Optional

import pandas as pd
import requests


def imf_indicators_to_dataframe(
    payload: Mapping[str, Any],
    *,
    indicator_key: str = "indicator_id",
) -> pd.DataFrame:
    """
    Turn IMF DataMapper ``GET .../api/v1/indicators`` JSON into a flat table.

    ``payload`` may be the full body (with a top-level ``"indicators"`` object)
    or that object alone: ``{"NGDP_RPCH": {"label": "...", ...}, ...}``.
    """
    raw = payload.get("indicators", payload) if isinstance(payload, Mapping) else {}
    if not isinstance(raw, dict):
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for code, info in raw.items():
        code_s = str(code).strip()
        if not code_s:
            continue
        row: dict[str, Any] = {indicator_key: code_s}
        if isinstance(info, Mapping):
            for field in ("label", "description", "dataset", "unit", "source"):
                row[field] = info.get(field)
        rows.append(row)

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fetch_imf_datamapper(
    indicator: str,
    *,
    countries: Optional[list[str]] = None,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
    timeout: int = 60,
) -> pd.DataFrame:
    """
    IMF DataMapper API helper.
    Example indicator: NGDP_RPCH, GGXWDG_NGDP
    """
    url = f"https://www.imf.org/external/datamapper/api/v1/{indicator}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()

    values = resp.json().get("values", {}).get(indicator, {})
    if not isinstance(values, dict):
        return pd.DataFrame()

    wanted = {c.strip().upper() for c in (countries or []) if c.strip()}
    records: list[dict[str, object]] = []
    for country_code, series in values.items():
        if wanted and country_code not in wanted:
            continue
        if not isinstance(series, dict):
            continue
        for year_s, raw in series.items():
            try:
                year = int(year_s)
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if start_year is not None and year < start_year:
                continue
            if end_year is not None and year > end_year:
                continue
            records.append(
                {
                    "indicator": indicator,
                    "country": country_code,
                    "year": year,
                    "value": val,
                }
            )
    if not records:
        return pd.DataFrame()
    return pd.DataFrame.from_records(records).sort_values(["country", "year"])

