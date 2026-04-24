from __future__ import annotations

from typing import Optional
import xml.etree.ElementTree as ET

import pandas as pd
import requests

from .api_keys import resolve_api_key

MOLIT_OPEN_API_URL = "http://stat.molit.go.kr/portal/openapi/service/rest/getList.do"
MOLIT_PUBLIC_DATA_URL = "https://stat.molit.go.kr/portal/stat/data.do"
MOLIT_PUBLIC_COLUMNS_URL = "https://stat.molit.go.kr/portal/stat/columns.do"

BUILDING_STATS_FORM_ID = "2202"
BUILDING_STATS_STYLE_NUM = "838"


def get_molit_api_key(
    api_key: Optional[str] = None, api_key_file: Optional[str] = None
) -> str:
    key = resolve_api_key(
        key_name="MOLIT",
        explicit_key=api_key,
        explicit_file=api_key_file,
        default_filename="molit_api_key.txt",
    )
    if not key:
        raise ValueError("MOLIT API key not found.")
    return key


def _xml_rows_to_dataframe(xml_text: str) -> pd.DataFrame:
    root = ET.fromstring(xml_text)
    rows = root.findall(".//row")
    records: list[dict[str, str]] = []
    for row in rows:
        rec: dict[str, str] = {}
        for child in row:
            rec[child.tag] = (child.text or "").strip()
        records.append(rec)
    return pd.DataFrame.from_records(records) if records else pd.DataFrame()


def fetch_molit_open_api(
    *,
    form_id: str,
    style_num: str,
    start_dt: str,
    end_dt: str,
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
    response_type: str = "json",
    timeout: int = 60,
) -> pd.DataFrame:
    """
    MOLIT statistics Open API fetcher.

    Parameters
    ----------
    form_id : str
        Statistics form id.
    style_num : str
        Style/group number required by the API.
    start_dt, end_dt : str
        Date range strings accepted by MOLIT API.
    response_type : str
        "json" or "xml". If json parsing fails, xml fallback is attempted.
    """
    key = get_molit_api_key(api_key=api_key, api_key_file=api_key_file)
    params = {
        "key": key,
        "form_id": form_id,
        "style_num": style_num,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }

    fmt = (response_type or "json").strip().lower()
    headers = {"Accept": "application/json" if fmt == "json" else "application/xml"}
    resp = requests.get(MOLIT_OPEN_API_URL, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()

    if fmt == "json":
        try:
            payload = resp.json()
        except ValueError:
            return _xml_rows_to_dataframe(resp.text)
        if isinstance(payload, dict):
            for key_name in ("row", "rows", "data", "list"):
                value = payload.get(key_name)
                if isinstance(value, list):
                    return pd.DataFrame(value)
            for value in payload.values():
                if isinstance(value, dict):
                    for key_name in ("row", "rows", "data", "list"):
                        nested = value.get(key_name)
                        if isinstance(nested, list):
                            return pd.DataFrame(nested)
        if isinstance(payload, list):
            return pd.DataFrame(payload)
        return pd.DataFrame()

    return _xml_rows_to_dataframe(resp.text)


def fetch_molit_public_stat(
    *,
    form_id: str,
    style_num: str,
    start_dt: str,
    end_dt: str,
    appr_yn: str = "Y",
    timeout: int = 60,
) -> pd.DataFrame:
    """
    Public MOLIT statistics endpoint used by the web UI.
    Does not require an API key for publicly exposed tables.
    """
    params = {
        "formId": form_id,
        "styleNum": style_num,
        "apprYn": appr_yn,
        "startDate": start_dt,
        "endDate": end_dt,
    }
    resp = requests.get(MOLIT_PUBLIC_DATA_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fetch_molit_public_columns(
    *, form_id: str, style_num: str, timeout: int = 60
) -> pd.DataFrame:
    params = {"formId": form_id, "styleNum": style_num}
    resp = requests.get(MOLIT_PUBLIC_COLUMNS_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fetch_molit_building_permit_stats(
    *, start_dt: str = "202001", end_dt: str = "202412", timeout: int = 60
) -> pd.DataFrame:
    """
    건축허가·착공·준공통계 public table fetcher.

    Source statistic page:
    https://stat.molit.go.kr/portal/cate/statView.do?hFormId=2202&hRsId=466
    """
    return fetch_molit_public_stat(
        form_id=BUILDING_STATS_FORM_ID,
        style_num=BUILDING_STATS_STYLE_NUM,
        start_dt=start_dt,
        end_dt=end_dt,
        timeout=timeout,
    )


def normalize_molit_column_names(
    df: pd.DataFrame,
    meta_df: pd.DataFrame,
    *,
    keep_original_numeric_columns: bool = False,
) -> pd.DataFrame:
    """
    Rename MOLIT numeric columns using metadata names from columns.do.

    Repeated names are suffixed as: "<name>_2", "<name>_3", ...
    """
    if df.empty or meta_df.empty:
        return df.copy()

    out = df.copy()
    rename_map: dict[str, str] = {}
    seen_names: dict[str, int] = {}

    for _, row in meta_df.iterrows():
        col_id = row.get("DATA_DIV_ID")
        col_name = str(row.get("DATA_DIV_NM", "")).strip()
        if pd.isna(col_id) or not col_name:
            continue
        source = str(int(col_id)) if not isinstance(col_id, str) else str(col_id).strip()
        if source not in out.columns:
            continue
        count = seen_names.get(col_name, 0) + 1
        seen_names[col_name] = count
        target = col_name if count == 1 else f"{col_name}_{count}"
        rename_map[source] = target

    if keep_original_numeric_columns:
        for source, target in rename_map.items():
            out[f"{target}__raw"] = out[source]
        return out

    return out.rename(columns=rename_map)

