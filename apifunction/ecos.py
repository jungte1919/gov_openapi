from __future__ import annotations

from typing import Optional

import pandas as pd
import requests

from .api_keys import resolve_api_key

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def get_ecos_api_key(
    api_key: Optional[str] = None, api_key_file: Optional[str] = None
) -> str:
    key = resolve_api_key(
        key_name="ECOS",
        explicit_key=api_key,
        explicit_file=api_key_file,
        default_filename="ecos_api_key.txt",
    )
    if not key:
        raise ValueError("ECOS API key not found.")
    return key


def fetch_ecos_statistic_item_list(
    stat_code: str,
    *,
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
    lang: str = "kr",
    timeout: int = 60,
) -> pd.DataFrame:
    key = get_ecos_api_key(api_key=api_key, api_key_file=api_key_file)
    base = "https://ecos.bok.or.kr/api"
    url = f"{base}/StatisticItemList/{key}/json/{lang}/1/100/{stat_code}"
    resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json().get("StatisticItemList", {}).get("row", [])
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def fetch_ecos_statistic_search(
    stat_code: str,
    *,
    cycle: str,
    start_time: str,
    end_time: str,
    item_code: str = "?",
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
    lang: str = "kr",
    timeout: int = 60,
) -> pd.DataFrame:
    key = get_ecos_api_key(api_key=api_key, api_key_file=api_key_file)
    base = "https://ecos.bok.or.kr/api"
    url = (
        f"{base}/StatisticSearch/{key}/json/{lang}/1/100000/"
        f"{stat_code}/{cycle}/{start_time}/{end_time}/{item_code}"
    )
    resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json().get("StatisticSearch", {}).get("row", [])
    return pd.DataFrame(rows) if rows else pd.DataFrame()

