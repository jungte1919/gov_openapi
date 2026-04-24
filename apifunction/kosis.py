# -*- coding: utf-8 -*-
"""
KOSIS Open API — Inequality `kosis_analysis/kosis.py` 와 동일한 호출·재시도·obj 레벨 패턴.

- **저수준**: ``fetch_kosis_statistics`` → 레코드 리스트
- **고수준**: ``fetch_kosis_dataframe`` (오류 31 시 구간 자동 분할)
- **편의**: ``fetch_kosis_table`` = 연도 구간 + 주기만 넘겨 ``fetch_kosis_dataframe`` 호출 (HTML 스크래핑 없음)

키는 ``api_keys.resolve_api_key`` 및 Base64·원문 후보(``resolve_kosis_api_key_candidates``)로 처리합니다.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests

from .api_keys import resolve_api_key

KOSIS_LIST_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
KOSIS_SEARCH_URL = "https://kosis.kr/search/searchStatDBAjax.do"

_KOSIS_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",
}


def _is_hex_api_key(s: str) -> bool:
    return len(s) >= 16 and all(c in "0123456789abcdefABCDEF-" for c in s)


def _looks_like_base64_text(s: str) -> bool:
    if "=" in s or "+" in s or "/" in s:
        return len(s.strip("=")) >= 8
    return False


def _try_decode_base64_api_key(value: str) -> str:
    s = value.strip().strip('"').strip("'")
    if not s:
        return ""
    if _is_hex_api_key(s.replace("-", "")):
        return s
    if not _looks_like_base64_text(s):
        return s
    try:
        raw = base64.b64decode(s, validate=True)
        decoded = raw.decode("ascii").strip()
        if decoded:
            return decoded
    except (ValueError, UnicodeDecodeError):
        pass
    return s


def _normalize_api_key_for_request(s: str) -> str:
    s = s.strip().strip('"').strip("'")
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff")
    return s


def resolve_kosis_api_key_candidates(
    api_key: Optional[str] = None,
    *,
    api_key_file: Optional[str] = None,
) -> list[str]:
    """
    KOSIS API 키 후보 [원문, Base64 디코드(다를 때만)] — Inequality ``kosis_credentials`` 와 동일 취지.
    """

    def unique_keep_order(items: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for x in items:
            x = str(x).strip()
            if x and x not in seen:
                out.append(x)
                seen.add(x)
        return out

    if api_key is not None:
        raw = _normalize_api_key_for_request(str(api_key))
        if not raw:
            return []
        decoded = _try_decode_base64_api_key(raw)
        return unique_keep_order([raw, decoded])

    raw = resolve_api_key(
        key_name="KOSIS",
        explicit_file=api_key_file,
        default_filename="kosis_api_key.txt",
    )
    if not raw:
        return []
    raw = _normalize_api_key_for_request(raw)
    decoded = _try_decode_base64_api_key(raw)
    return unique_keep_order([raw, decoded])


def get_kosis_api_key(
    api_key: Optional[str] = None, api_key_file: Optional[str] = None
) -> str:
    """첫 번째 유효 키(후보 첫 항목). 없으면 ``ValueError``."""
    cands = resolve_kosis_api_key_candidates(api_key, api_key_file=api_key_file)
    if not cands:
        raise ValueError("KOSIS API key not found.")
    return cands[0]


def _kosis_http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_KOSIS_HEADERS)
    return s


def _sleep_before_retry(attempt: int) -> None:
    time.sleep(min(8.0, 0.35 * (2**attempt)))


_CONNECTION_RESET_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


def _kosis_get_json(
    sess: requests.Session,
    url: str,
    *,
    params: dict[str, str],
    timeout: float,
) -> Any:
    for attempt in range(5):
        try:
            r = sess.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except _CONNECTION_RESET_ERRORS:
            if attempt >= 4:
                raise
            _sleep_before_retry(attempt)


def _kosis_err_code(data: Any) -> int | None:
    if isinstance(data, dict) and "err" in data:
        try:
            return int(data["err"])
        except (ValueError, TypeError):
            return None
    return None


def _normalize_kosis_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "err" in data:
            err = data.get("err")
            msg = data.get("errMsg", data)
            tip = ""
            if err == 21:
                tip = (
                    " | 점검: prdSe가 통계표 자료주기와 같은지(연 Y·분기 Q·월 M 등), "
                    "orgId가 표 화면 URL과 같은지, 구간 조회 시 start/end 형식이 주기에 맞는지 "
                    "(연간은 2019, 분기는 202301~202304 형태)."
                )
            raise RuntimeError(f"KOSIS 오류 {err}: {msg}{tip}")
        if "StatisticalData" in data:
            inner = data["StatisticalData"]
            if isinstance(inner, list):
                return inner
            raise TypeError(f"StatisticalData 형식이 list가 아님: {type(inner)}")
    raise TypeError(f"예상하지 못한 응답 형식: {type(data)}")


def lookup_org_id(tbl_id: str, timeout: float = 60) -> str:
    tid = str(tbl_id).strip().upper()
    with _kosis_http_session() as sess:
        for gbn in ("L", "E", "I", "B"):
            payload: dict[str, Any] = {}
            for attempt in range(5):
                try:
                    r = sess.post(
                        KOSIS_SEARCH_URL,
                        data={"query": tid, "gbn": gbn},
                        timeout=timeout,
                    )
                    r.raise_for_status()
                    payload = r.json()
                    break
                except _CONNECTION_RESET_ERRORS:
                    if attempt >= 4:
                        raise
                    _sleep_before_retry(attempt)
            rows = payload.get("resultList") or []
            if rows and rows[0].get("ORG_ID"):
                return str(rows[0]["ORG_ID"])
    raise LookupError(
        f"ORG_ID 를 찾지 못했습니다. tblId={tid!r} 및 KOSIS 검색을 확인하세요."
    )


def fetch_kosis_statistics(
    tbl_id: str,
    *,
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
    org_id: Optional[str] = None,
    prd_se: str = "Y",
    new_est_prd_cnt: Optional[int] = 48,
    prd_interval: int = 1,
    start_prd_de: Optional[str] = None,
    end_prd_de: Optional[str] = None,
    load_gubun: str = "2",
    itm_id: str = "ALL",
    obj_l1: str = "ALL",
    obj_l2: str = "",
    obj_l3: str = "",
    obj_l4: str = "",
    obj_l5: str = "",
    obj_l6: str = "",
    obj_l7: str = "",
    obj_l8: str = "",
    timeout: float = 120,
) -> list[dict[str, Any]]:
    key_candidates = resolve_kosis_api_key_candidates(api_key, api_key_file=api_key_file)
    if not key_candidates:
        raise ValueError(
            "API 키가 없습니다. 환경변수 KOSIS_API_KEY 또는 "
            "kosis_api_key.txt 등을 설정하세요."
        )

    tid = str(tbl_id).strip().upper()
    prd = str(prd_se).strip().upper()

    if org_id is not None and str(org_id).strip():
        oid = str(org_id).strip()
    elif len(tid) > 6 and tid[3:6].isdigit():
        oid = str(int(tid[3:6]))
    else:
        oid = lookup_org_id(tid)

    user_objl = [obj_l1, obj_l2, obj_l3, obj_l4, obj_l5, obj_l6, obj_l7, obj_l8]
    explicit_count = sum(1 for v in user_objl if v.strip())

    base_params: dict[str, str] = {
        "method": "getList",
        "format": "json",
        "jsonVD": "Y",
        "orgId": oid,
        "tblId": tid,
        "prdSe": prd,
        "itmId": itm_id,
    }

    use_range = start_prd_de is not None and end_prd_de is not None
    if use_range:
        base_params["startPrdDe"] = str(start_prd_de).strip()
        base_params["endPrdDe"] = str(end_prd_de).strip()
        base_params["loadGubun"] = str(load_gubun)
    else:
        if new_est_prd_cnt is None:
            raise ValueError(
                "start_prd_de/end_prd_de 가 없으면 new_est_prd_cnt 를 지정하세요."
            )
        base_params["newEstPrdCnt"] = str(int(new_est_prd_cnt))
        base_params["prdInterval"] = str(int(prd_interval))

    def _build_params_with_objl(n_levels: int) -> dict[str, str]:
        p = dict(base_params)
        for i in range(n_levels):
            key = f"objL{i + 1}"
            val = user_objl[i].strip() if user_objl[i].strip() else "ALL"
            p[key] = val
        return p

    if explicit_count > 0:
        level_attempts = [explicit_count]
    else:
        level_attempts = [1]

    last_payload: Any = None

    with _kosis_http_session() as sess:
        for key in key_candidates:
            got_invalid_key = False

            tried_levels: set[int] = set()
            attempt_queue = list(level_attempts)

            while attempt_queue:
                n_lvl = attempt_queue.pop(0)
                if n_lvl in tried_levels or n_lvl < 1 or n_lvl > 8:
                    continue
                tried_levels.add(n_lvl)

                params = _build_params_with_objl(n_lvl)
                params["apiKey"] = key

                for retry in range(3):
                    try:
                        payload = _kosis_get_json(
                            sess, KOSIS_LIST_URL, params=params, timeout=timeout
                        )
                    except requests.RequestException:
                        if retry >= 2:
                            raise
                        time.sleep(0.4)
                        continue

                    last_payload = payload
                    err_code = _kosis_err_code(payload)

                    if err_code == 11:
                        got_invalid_key = True
                        break

                    if err_code == 20:
                        for up in range(n_lvl + 1, min(n_lvl + 4, 9)):
                            if up not in tried_levels:
                                attempt_queue.insert(0, up)
                        break

                    if err_code == 21:
                        if n_lvl > 1:
                            for down in range(n_lvl - 1, 0, -1):
                                if down not in tried_levels:
                                    attempt_queue.insert(0, down)
                        break

                    return _normalize_kosis_payload(payload)

                if got_invalid_key:
                    break

            if got_invalid_key:
                continue

    if _kosis_err_code(last_payload) == 11:
        return _normalize_kosis_payload(last_payload)
    return _normalize_kosis_payload(last_payload)


def kosis_records_to_dataframe(records: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    if df.empty:
        return df
    val_col = "DT" if "DT" in df.columns else next(
        (c for c in df.columns if c.upper() in ("DATA_VAL", "VAL", "DATA")), None
    )
    if val_col and val_col != "DT" and "DT" not in df.columns:
        df = df.rename(columns={val_col: "DT"})
    if "DT" in df.columns:
        df["DT"] = (
            df["DT"]
            .astype(str)
            .str.replace(",", "", regex=False)
            .replace({"": np.nan, "-": np.nan})
        )
        df["DT"] = pd.to_numeric(df["DT"], errors="coerce")
    return df


def _generate_year_chunks(
    start: str, end: str, prd: str, chunk_years: int = 5
):
    s_int = int(start[:4])
    e_int = int(end[:4])
    suffix_s = start[4:] or ""
    suffix_e = end[4:] or ""
    cur = s_int
    while cur <= e_int:
        c_end = min(cur + chunk_years - 1, e_int)
        cs = (
            f"{cur}{suffix_s}"
            if cur == s_int
            else str(cur)
            + (
                "01"
                if prd in ("Q", "H")
                else "01"
                if prd == "M"
                else ""
            )
        )
        if prd == "Q":
            ce = (
                f"{c_end}04"
                if c_end < e_int
                else f"{c_end}{suffix_e}" if suffix_e else f"{c_end}04"
            )
        elif prd == "M":
            ce = (
                f"{c_end}12"
                if c_end < e_int
                else f"{c_end}{suffix_e}" if suffix_e else f"{c_end}12"
            )
        elif prd == "H":
            ce = (
                f"{c_end}02"
                if c_end < e_int
                else f"{c_end}{suffix_e}" if suffix_e else f"{c_end}02"
            )
        else:
            ce = (
                str(c_end)
                if c_end < e_int
                else f"{c_end}{suffix_e}" if suffix_e else str(c_end)
            )
        yield cs, ce
        cur = c_end + 1


def fetch_kosis_dataframe(
    tbl_id: str,
    *,
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
    org_id: Optional[str] = None,
    prd_se: str = "Y",
    new_est_prd_cnt: Optional[int] = 48,
    prd_interval: int = 1,
    start_prd_de: Optional[str] = None,
    end_prd_de: Optional[str] = None,
    load_gubun: str = "2",
    itm_id: str = "ALL",
    obj_l1: str = "ALL",
    obj_l2: str = "",
    obj_l3: str = "",
    obj_l4: str = "",
    obj_l5: str = "",
    obj_l6: str = "",
    obj_l7: str = "",
    obj_l8: str = "",
    timeout: float = 120,
) -> pd.DataFrame:
    """
    ``fetch_kosis_statistics`` + ``kosis_records_to_dataframe``.
    오류 31(40,000셀 초과) 시 구간을 자동 분할하여 재시도합니다.
    """
    common_kw = dict(
        api_key=api_key,
        api_key_file=api_key_file,
        org_id=org_id,
        load_gubun=load_gubun,
        itm_id=itm_id,
        obj_l1=obj_l1,
        obj_l2=obj_l2,
        obj_l3=obj_l3,
        obj_l4=obj_l4,
        obj_l5=obj_l5,
        obj_l6=obj_l6,
        obj_l7=obj_l7,
        obj_l8=obj_l8,
        timeout=timeout,
    )
    prd_upper = (prd_se or "Y").strip().upper()

    def _fetch_dataframe_for_prd(prd_code: str) -> pd.DataFrame:
        try:
            records = fetch_kosis_statistics(
                tbl_id,
                prd_se=prd_code,
                new_est_prd_cnt=new_est_prd_cnt,
                prd_interval=prd_interval,
                start_prd_de=start_prd_de,
                end_prd_de=end_prd_de,
                **common_kw,
            )
            return kosis_records_to_dataframe(records)
        except RuntimeError as e:
            if "31" not in str(e):
                raise

        st = start_prd_de or "2000"
        en = end_prd_de or "2027"
        if not start_prd_de and not end_prd_de:
            _defaults = {
                "Y": ("2000", "2027"),
                "F": ("2000", "2027"),
                "Q": ("200001", "202704"),
                "M": ("200001", "202712"),
                "H": ("200001", "202702"),
            }
            st, en = _defaults.get(prd_code, ("2000", "2027"))

        all_dfs: list[pd.DataFrame] = []
        chunk_years = 5
        while chunk_years >= 1:
            all_dfs.clear()
            success = True
            for cs, ce in _generate_year_chunks(st, en, prd_code, chunk_years):
                try:
                    recs = fetch_kosis_statistics(
                        tbl_id,
                        prd_se=prd_code,
                        new_est_prd_cnt=None,
                        prd_interval=prd_interval,
                        start_prd_de=cs,
                        end_prd_de=ce,
                        **common_kw,
                    )
                    df_chunk = kosis_records_to_dataframe(recs)
                    if not df_chunk.empty:
                        all_dfs.append(df_chunk)
                except RuntimeError as e2:
                    if "31" in str(e2) and chunk_years > 1:
                        chunk_years = max(1, chunk_years // 2)
                        success = False
                        break
                    if "30" in str(e2):
                        continue
                    raise
            if success:
                break

        if not all_dfs:
            return pd.DataFrame()
        result = pd.concat(all_dfs, ignore_index=True)
        if "PRD_DE" in result.columns:
            result = result.drop_duplicates().sort_values("PRD_DE").reset_index(drop=True)
        return result

    prd_candidates = [prd_upper]
    if prd_upper == "Y":
        prd_candidates.append("F")

    last_error: RuntimeError | None = None
    for idx, prd_code in enumerate(prd_candidates):
        try:
            df = _fetch_dataframe_for_prd(prd_code)
        except RuntimeError as exc:
            last_error = exc
            if "30" in str(exc) and idx < len(prd_candidates) - 1:
                continue
            raise
        if not df.empty or idx == len(prd_candidates) - 1:
            return df

    if last_error is not None:
        raise last_error
    return pd.DataFrame()


def _years_to_prd_de_range(start_year: int, end_year: int, prd: str) -> tuple[str, str]:
    prd = prd.upper()
    if prd in ("Y", "F"):
        return str(start_year), str(end_year)
    if prd == "M":
        return f"{start_year}01", f"{end_year}12"
    if prd == "Q":
        return f"{start_year}01", f"{end_year}04"
    if prd == "H":
        return f"{start_year}01", f"{end_year}02"
    return str(start_year), str(end_year)


def fetch_kosis_table(
    tbl_id: str,
    *,
    cycle: str = "A",
    start_year: int = 2000,
    end_year: Optional[int] = None,
    org_id: Optional[str] = None,
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
) -> pd.DataFrame:
    """
    연도 구간·주기만 지정하는 편의 래퍼. 내부적으로 ``fetch_kosis_dataframe`` (구간 모드) 사용.
    """
    if end_year is None:
        end_year = datetime.now().year
    prd_se = {"A": "Y", "Y": "Y", "M": "M", "Q": "Q", "H": "H", "F": "F"}.get(
        cycle.upper(), "Y"
    )
    st, en = _years_to_prd_de_range(start_year, end_year, prd_se)
    return fetch_kosis_dataframe(
        tbl_id,
        api_key=api_key,
        api_key_file=api_key_file,
        org_id=org_id,
        prd_se=prd_se,
        new_est_prd_cnt=None,
        start_prd_de=st,
        end_prd_de=en,
    )


__all__ = [
    "fetch_kosis_statistics",
    "fetch_kosis_dataframe",
    "fetch_kosis_table",
    "get_kosis_api_key",
    "kosis_records_to_dataframe",
    "lookup_org_id",
    "resolve_kosis_api_key_candidates",
]
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

try:
    from .api_keys import resolve_api_key
except ImportError:
    import sys
    from pathlib import Path

    _HERE = Path(__file__).resolve().parent
    _ROOT = _HERE.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from apifunction.api_keys import resolve_api_key


def get_kosis_api_key(
    api_key: Optional[str] = None, api_key_file: Optional[str] = None
) -> str:
    key = resolve_api_key(
        key_name="KOSIS",
        explicit_key=api_key,
        explicit_file=api_key_file,
        default_filename="kosis_api_key.txt",
    )
    if not key:
        raise ValueError("KOSIS API key not found.")
    return key


class KosisParamDownloader:
    labels = {
        "Y": "Yearly",
        "F": "Biennial",
        "M": "Monthly",
        "Q": "Quarterly",
        "H": "Half-year",
    }

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.org_id: str = ""
        self.tbl_id: str = ""
        self.prd_se: str = "Y"
        self.objs: str = ""
        self.options: set[int] = set()
        self.st = 0
        self.ed = 0

    def _adjust_period(self) -> tuple[int, int]:
        if self.prd_se in {"M", "Q", "H"}:
            ends = {"M": 12, "Q": 4, "H": 2}
            st = int(self.st * 100 + 1)
            ed = int(self.ed * 100 + ends[self.prd_se])
        else:
            st, ed = self.st, self.ed
        min_o, max_o = min(self.options), max(self.options)
        if st > max_o or ed < min_o:
            raise ValueError("Requested period is out of available range.")
        return max(st, min_o), min(ed, max_o)

    def _get_org_id(self) -> None:
        for gbn in ("L", "E", "I", "B"):
            try:
                r = requests.post(
                    "https://kosis.kr/search/searchStatDBAjax.do",
                    data={"query": self.tbl_id, "gbn": gbn},
                    timeout=15,
                )
                r.raise_for_status()
                rl = (r.json() or {}).get("resultList") or []
                if rl and rl[0].get("ORG_ID"):
                    self.org_id = str(rl[0]["ORG_ID"])
                    return
            except (requests.RequestException, ValueError, KeyError):
                continue
        raise AttributeError(f"ORG_ID lookup failed for table {self.tbl_id}")

    def _init_data(self) -> None:
        self._get_org_id()
        r = requests.get(
            f"https://kosis.kr/statHtml/statHtmlContent.do?orgId={self.org_id}&tblId={self.tbl_id}",
            timeout=20,
        )
        r.raise_for_status()
        obj_count = len(re.findall(r'var tempMaxLvl\s*=\s*"\d";', r.text))
        self.objs = "".join(f"&objL{i}=ALL" for i in range(1, obj_count + 1))
        soup = BeautifulSoup(r.text, "html.parser")
        span = soup.find("span", class_="top", id=f"time{self.prd_se}")
        if span is None:
            raise TypeError(f"Cycle {self.prd_se} is not available for {self.tbl_id}.")
        self.options = {int(o["value"]) for o in span.find_all("option")}

    @staticmethod
    def _build_obj_query(
        obj_count: int,
        obj_filters: Optional[dict[int, str]] = None,
    ) -> str:
        filters = obj_filters or {}
        parts: list[str] = []
        for i in range(1, obj_count + 1):
            value = str(filters.get(i, "ALL")).strip() or "ALL"
            parts.append(f"&objL{i}={value}")
        return "".join(parts)

    def fetch(
        self,
        *,
        start_year: int,
        end_year: int,
        tbl_id: str,
        prd_se: str = "Y",
        obj_filters: Optional[dict[int, str]] = None,
    ) -> pd.DataFrame:
        self.tbl_id = str(tbl_id).upper().strip()
        self.prd_se = str(prd_se).upper().strip()
        self.st = int(start_year)
        self.ed = int(end_year)
        self._init_data()
        obj_count = self.objs.count("&objL")
        self.objs = self._build_obj_query(obj_count, obj_filters=obj_filters)
        st_adj, ed_adj = self._adjust_period()
        option_list = sorted(self.options)
        result: list[dict] = []
        for option in option_list[option_list.index(st_adj) : option_list.index(ed_adj) + 1]:
            url = (
                "https://kosis.kr/openapi/Param/statisticsParameterData.do?"
                f"method=getList&apiKey={self.api_key}&tblId={self.tbl_id}&orgId={self.org_id}"
                f"&startPrdDe={option}&endPrdDe={option}&itmId=ALL&format=json&jsonVD=Y"
                f"&prdSe={self.prd_se}&loadGubun=2{self.objs}"
            )
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            chunk = resp.json()
            if isinstance(chunk, dict) and chunk.get("err"):
                code = str(chunk.get("err", "")).strip()
                msg = str(chunk.get("errMsg", "")).strip()
                raise RuntimeError(
                    f"KOSIS API error for {self.tbl_id} ({self.prd_se}, {option}): "
                    f"err={code}, errMsg={msg}. "
                    "Narrow dimensions with obj_filters (e.g. {1:'...',2:'...'})."
                )
            if isinstance(chunk, list):
                result.extend(chunk)
        if not result:
            return pd.DataFrame()
        return pd.DataFrame(result).drop_duplicates()


def fetch_kosis_table(
    tbl_id: str,
    *,
    cycle: str = "A",
    start_year: int = 2000,
    end_year: Optional[int] = None,
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
    obj_filters: Optional[dict[int, str]] = None,
) -> pd.DataFrame:
    if end_year is None:
        end_year = datetime.now().year
    key = get_kosis_api_key(api_key=api_key, api_key_file=api_key_file)
    prd_se = {"A": "Y", "Y": "Y", "F": "F", "M": "M", "Q": "Q", "H": "H"}.get(
        cycle.upper(), "Y"
    )
    return KosisParamDownloader(key).fetch(
        start_year=start_year,
        end_year=end_year,
        tbl_id=tbl_id,
        prd_se=prd_se,
        obj_filters=obj_filters,
    )

