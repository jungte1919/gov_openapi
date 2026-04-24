from __future__ import annotations

from typing import Optional
import xml.etree.ElementTree as ET

import pandas as pd
import requests

from .api_keys import resolve_api_key


def get_openfiscal_api_key(
    api_key: Optional[str] = None, api_key_file: Optional[str] = None
) -> str:
    key = resolve_api_key(
        key_name="OPENFISCAL",
        explicit_key=api_key,
        explicit_file=api_key_file,
        default_filename="openfiscal_api_key.txt",
    )
    if not key:
        raise ValueError("OpenFiscal API key not found.")
    return key


def fetch_openfiscal_service(
    service_id: str,
    *,
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
    extra_params: Optional[dict[str, str]] = None,
    page_size: int = 1000,
    timeout: int = 60,
) -> pd.DataFrame:
    """
    OpenFiscal Open API XML downloader.
    Example service_id: OPFI152
    extra_params example: {"ACNT_YR": "2024", "OFFC_CD": "001"}
    """
    key = get_openfiscal_api_key(api_key=api_key, api_key_file=api_key_file)
    url = f"https://openapi.openfiscaldata.go.kr/{service_id}"

    rows: list[dict[str, str]] = []
    page = 1
    while True:
        params = {"Key": key, "Type": "xml", "pIndex": page, "pSize": page_size}
        if extra_params:
            params.update(extra_params)
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        page_rows = root.findall(".//row")
        if not page_rows:
            break

        for row in page_rows:
            rec: dict[str, str] = {}
            for child in row:
                rec[child.tag] = (child.text or "").strip()
            rows.append(rec)

        if len(page_rows) < page_size:
            break
        page += 1

    return pd.DataFrame.from_records(rows) if rows else pd.DataFrame()

