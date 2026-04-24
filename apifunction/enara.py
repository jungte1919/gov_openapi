from __future__ import annotations

from typing import Any
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ENARA_API_URL = "https://www.index.go.kr/openApi/xml_stts.do"
ENARA_ID = "229151DF51U114T0"


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _build_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods={"GET"},
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.headers.update({"User-Agent": "enara-python/1.0"})
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _parse_xml_to_dataframe(xml_payload: bytes | str) -> tuple[pd.DataFrame, dict[str, str]]:
    # Parse from bytes when possible so XML declaration encoding is honored.
    root = ET.fromstring(xml_payload)
    indicator_meta: dict[str, str] = {}
    data_rows: list[dict[str, Any]] = []

    for child in root:
        if _local_name(child.tag) != "통계표":
            indicator_meta[_local_name(child.tag)] = (child.text or "").strip()

    for table in root.findall("./통계표"):
        table_meta: dict[str, Any] = {}
        for node in table:
            nm = _local_name(node.tag)
            if nm != "표":
                table_meta[nm] = (node.text or "").strip()

        for period_node in table.findall("./표"):
            period = period_node.attrib.get("주기", "")
            # Some ENARA tables nest items under <항목그룹>.
            for item_node in period_node.findall(".//항목"):
                item_code = item_node.attrib.get("코드", "")
                item_name = item_node.attrib.get("이름", "")
                # Some tables nest <열> under additional group nodes (e.g. <분류1>).
                for col in item_node.findall(".//열"):
                    row: dict[str, Any] = {
                        "주기": period,
                        "항목코드": item_code,
                        "항목명": item_name,
                        **table_meta,
                    }
                    for k, v in col.attrib.items():
                        row[k] = v
                    value = (col.text or "").strip()
                    if value:
                        row["값"] = value
                    data_rows.append(row)

    df = pd.DataFrame(data_rows)
    if not df.empty:
        if "시점" in df.columns:
            df["시점"] = pd.to_numeric(df["시점"], errors="ignore")
        if "값" in df.columns:
            df["값"] = pd.to_numeric(df["값"], errors="coerce")
    return df, indicator_meta


def fetch_enara_table(
    stats_code: int | str, indicator_code: int | str | None = None
) -> pd.DataFrame:
    stats_code_str = str(stats_code).strip()
    if not stats_code_str.isdigit():
        raise ValueError("stats_code must be numeric.")

    if indicator_code is not None:
        ind = str(indicator_code).strip()
        if ind and not stats_code_str.startswith(ind):
            raise ValueError(
                f"stats_code({stats_code_str}) does not start with indicator_code({ind})."
            )

    session = _build_session()
    resp = session.get(
        ENARA_API_URL,
        params={"idntfcId": ENARA_ID, "statsCode": stats_code_str},
        timeout=30,
    )
    resp.raise_for_status()

    df, indicator_meta = _parse_xml_to_dataframe(resp.content)
    if df.empty:
        raise RuntimeError(f"No ENARA data for stats_code={stats_code_str}")
    df.attrs["indicator"] = indicator_meta
    return df

