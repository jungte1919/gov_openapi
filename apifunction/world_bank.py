from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd
import requests

DEFAULT_COUNTRIES = ["KOR", "USA", "JPN", "GBR", "IDN", "DEU", "CHN"]


def _fetch_pages(url: str, params: dict, timeout: int = 90) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        p = {**params, "page": page}
        r = requests.get(url, params=p, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            break
        chunk = data[1]
        if not chunk:
            break
        out.extend(chunk)
        meta = data[0]
        if isinstance(meta, dict) and page >= int(meta.get("pages", 1)):
            break
        page += 1
    return out


def fetch_wb_indicator_panel(
    *,
    indicator: str = "NE.TRD.GNFS.ZS",
    countries: Optional[list[str]] = None,
    start_year: int = 2000,
    end_year: Optional[int] = None,
    timeout: int = 90,
) -> pd.DataFrame:
    if end_year is None:
        end_year = datetime.now().year
    if countries is None:
        country_path = "all"
    else:
        iso = [c.strip().upper() for c in countries if c and c.strip()]
        if not iso:
            return pd.DataFrame()
        country_path = ";".join(iso)
    url = f"https://api.worldbank.org/v2/country/{country_path}/indicator/{indicator}"
    params = {"date": f"{start_year}:{end_year}", "format": "json", "per_page": 20000}
    rows = _fetch_pages(url, params, timeout=timeout)
    recs: list[dict[str, object]] = []
    for row in rows:
        iso3 = (row.get("countryiso3code") or "").strip().upper()
        if not iso3:
            country_obj = row.get("country") or {}
            if isinstance(country_obj, dict):
                iso3 = str(country_obj.get("id") or "").strip().upper()
        year = row.get("date")
        value = row.get("value")
        if not iso3 or year is None or value is None:
            continue
        try:
            recs.append({"country": iso3, "year": int(year), "value": float(value)})
        except (TypeError, ValueError):
            continue
    return pd.DataFrame.from_records(recs)

