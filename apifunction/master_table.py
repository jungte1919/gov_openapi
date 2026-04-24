from __future__ import annotations

import argparse
import ast
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse
from typing import Any, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from tqdm.auto import tqdm
except ImportError:
    tqdm = None

try:
    from .api_keys import resolve_api_key
    from .ecos import fetch_ecos_statistic_search
    from .enara import fetch_enara_table
    from .excel_io import excel_engine_from_signature, peek_excel_head
    from .imf import fetch_imf_datamapper, imf_indicators_to_dataframe
    from .kosis import fetch_kosis_table
    from .molit import fetch_molit_open_api, fetch_molit_public_stat
    from .openfiscal import fetch_openfiscal_service
except ImportError:
    # Support notebook/local script execution without package context.
    import sys
    from pathlib import Path

    _HERE = Path(__file__).resolve().parent
    _ROOT = _HERE.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    from apifunction.api_keys import resolve_api_key
    from apifunction.ecos import fetch_ecos_statistic_search
    from apifunction.enara import fetch_enara_table
    from apifunction.excel_io import excel_engine_from_signature, peek_excel_head
    from apifunction.imf import fetch_imf_datamapper, imf_indicators_to_dataframe
    from apifunction.kosis import fetch_kosis_table
    from apifunction.molit import fetch_molit_open_api, fetch_molit_public_stat
    from apifunction.openfiscal import fetch_openfiscal_service

ECOS_TABLE_LIST_URL = "https://ecos.bok.or.kr/api/StatisticTableList"
IMF_INDICATOR_URL = "https://www.imf.org/external/datamapper/api/v1/indicators"
WORLD_BANK_INDICATOR_URL = "https://api.worldbank.org/v2/indicator"
OPENFISCAL_LIST_URL = "https://www.openfiscaldata.go.kr/op/ko/sd/dtsStats/selectScolSrchList.do"
OPENFISCAL_YEAR_LIST_URL = "https://www.openfiscaldata.go.kr/op/ko/cm/selectYrList.do"
ENARA_OFFICE_LIST_URL = "https://www.index.go.kr/unity/potal/eNara/sub/EnaraSystemOffcIntro.do"
ENARA_DETAIL_URL = "https://www.index.go.kr/unity/potal/main/EachDtlPageDetail.do"
ENARA_XML_INDEX_URL = "https://www.index.go.kr/unity/openApi/xml_idx.do"
MOLIT_PARTIAL_LIST_URL = "https://stat.molit.go.kr/portal/cate/partSttsAjx.do"
MOLIT_STAT_VIEW_URL = "https://stat.molit.go.kr/portal/cate/statView.do"
MOLIT_CATEGORY_CODES = {
    "0120000": "국토/도시",
    "0150000": "주택",
    "0590000": "토지",
    "0130000": "건설",
    "0160000": "교통/물류",
    "0170000": "항공",
    "0180000": "도로/철도",
}

_CYCLE_MAP = {
    "연": "A",
    "년": "Y",
    "월": "M",
    "분기": "Q",
    "반기": "H",
    "격년": "F",
    "격별": "F",
    "격": "F",
    "1년": "F",
    "2년": "F",
    "3년": "F",
    "4년": "F",
    "5년": "F",
    "10년": "F",
    "부정기": "IR"
}

MASTER_COLUMNS = [
    "index",
    "source",
    "table_id",
    "table_name",
    "cycle",
    "start_date",
    "end_date",
    "params",
]


def _normalize_cycle(raw: Any) -> Optional[str]:
    if raw is None or pd.isna(raw):
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if s in {"A", "Y", "M", "Q", "H", "F"}:
        return s
    for prefix, code in _CYCLE_MAP.items():
        if str(raw).strip().startswith(prefix):
            return code
    return None


def _extract_period_tokens(raw: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse values like "월 (199301 ~ 200912)" into ("199301", "200912", "M").
    Keeps original tokens as strings (no datetime conversion).
    """
    if raw is None or pd.isna(raw):
        return None, None, None
    s = str(raw).strip()
    if not s:
        return None, None, None

    cycle = None
    for prefix, code in _CYCLE_MAP.items():
        if s.startswith(prefix):
            cycle = code
            break

    m = re.search(r"[\(（]\s*([^)）]+)\s*[\)）]", s)
    if not m:
        return None, None, cycle

    parts = [p.strip() for p in re.split(r"\s*[~～－]\s*", m.group(1)) if p.strip()]
    if not parts:
        return None, None, cycle
    return parts[0], parts[-1], cycle


def _detect_kosis_header_row(excel_path: str, engine: str, sheet_name: str) -> int:
    preview = pd.read_excel(
        excel_path,
        sheet_name=sheet_name,
        engine=engine,
        header=None,
        nrows=15,
    )
    for idx in range(len(preview)):
        row_text = " ".join(str(v) for v in preview.iloc[idx].tolist() if v is not None)
        row_upper = row_text.upper()
        if "TBL_ID" in row_upper or ("LEVEL" in row_upper and "LIST_ID" in row_upper):
            return idx
    return 0


def build_kosis_master_table(
    excel_path: str,
    *,
    max_sheets: int = 5,
    header_row: Optional[int] = None,
) -> pd.DataFrame:
    engine = excel_engine_from_signature(peek_excel_head(excel_path))
    xls = pd.ExcelFile(excel_path, engine=engine)
    sheet_names = xls.sheet_names[:max_sheets]
    if not sheet_names:
        return pd.DataFrame()

    actual_header = (
        _detect_kosis_header_row(excel_path, engine, sheet_names[0])
        if header_row is None
        else int(header_row)
    )

    base = pd.read_excel(
        excel_path,
        sheet_name=sheet_names[0],
        engine=engine,
        header=actual_header,
    )
    cols = base.columns
    frames = [base]
    for sheet in sheet_names[1:]:
        frame = pd.read_excel(
            excel_path,
            sheet_name=sheet,
            engine=engine,
            header=actual_header,
        ).reindex(columns=cols)
        frames.append(frame)
    merged = pd.concat(frames, ignore_index=True)

    out = merged.copy()
    tbl_col = next((c for c in out.columns if "TBL_ID" in str(c)), "통계표 아이디(TBL_ID)")
    title_col = "통계명" if "통계명" in out.columns else None
    out["table_id"] = out.get(tbl_col)
    out["table_name"] = out.get(title_col) if title_col else None
    periods = out.get("수록기간", pd.Series([None] * len(out))).map(_extract_period_tokens)
    out["start_date"] = [a for a, _, _ in periods]
    out["end_date"] = [b for _, b, _ in periods]
    out["cycle"] = [c for _, _, c in periods]
    out["cycle"] = out["cycle"].fillna(out.get("주기", pd.Series([None] * len(out))).map(_normalize_cycle))
    if "통계표조회" in out.columns:
        out = out[out["통계표조회"].eq("통계표 보기")]
    out = out[out["table_id"].notna()].copy()
    out["index"] = "kosis:" + out["table_id"].astype(str)
    out["params"] = out.apply(
        lambda r: {
            "tbl_id": str(r["table_id"]),
            "cycle": r.get("cycle"),
            "start_date": r.get("start_date"),
            "end_date": r.get("end_date"),
        },
        axis=1,
    )
    return _ensure_master_columns(out)


def build_ecos_master_table(
    *,
    api_key: Optional[str] = None,
    api_key_file: Optional[str] = None,
    lang: str = "kr",
    timeout: int = 60,
) -> pd.DataFrame:
    key = resolve_api_key(
        key_name="ECOS",
        explicit_key=api_key,
        explicit_file=api_key_file,
        default_filename="ecos_api_key.txt",
    )
    if not key:
        raise ValueError("ECOS API key not found. Set ECOS_API_KEY or ecos_api_key.txt.")
    url = f"{ECOS_TABLE_LIST_URL}/{key}/json/{lang}/1/2000/"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json().get("StatisticTableList", {}).get("row", [])
    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    if df.empty:
        return df
    out = df.copy()
    out["table_id"] = out.get("STAT_CODE")
    out["table_name"] = out.get("STAT_NAME")
    out["start_date"] = None
    out["end_date"] = None
    out["cycle"] = out.get("CYCLE").map(_normalize_cycle)
    out = out[out["table_id"].notna()].copy()
    out["index"] = "ecos:" + out["table_id"].astype(str)
    out["params"] = out.apply(
        lambda r: {
            "stat_code": str(r["table_id"]),
            "cycle": r.get("cycle"),
            "start_time": r.get("start_date"),
            "end_time": r.get("end_date"),
        },
        axis=1,
    )
    return _ensure_master_columns(out)


def build_imf_master_table(*, timeout: int = 60) -> pd.DataFrame:
    resp = requests.get(IMF_INDICATOR_URL, timeout=timeout)
    resp.raise_for_status()
    base = imf_indicators_to_dataframe(resp.json(), indicator_key="dataset_id")
    if base.empty:
        return base
    out = base.copy()
    out["table_id"] = out.get("dataset_id")
    out["table_name"] = out.get("label")
    out["start_date"] = None
    out["end_date"] = None
    out["cycle"] = "Y"
    out = out[out["table_id"].notna()].copy()
    out["index"] = "imf:" + out["table_id"].astype(str)
    out["params"] = out["table_id"].map(
        lambda x: {"indicator": str(x), "countries": None, "start_year": None, "end_year": None}
    )
    return _ensure_master_columns(out)


def _param_default(params: Any, key: str) -> Optional[str]:
    if not isinstance(params, list):
        return None
    for item in params:
        if isinstance(item, dict) and str(item.get("name", "")).strip() == key:
            value = item.get("default")
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return None
            return str(value)
    return None


def _to_master_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=MASTER_COLUMNS)
    df = pd.DataFrame(rows)
    if "source" not in df.columns and "index" in df.columns:
        df["source"] = df["index"].map(_source_from_index)
    for col in MASTER_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[MASTER_COLUMNS].copy()


def _ensure_master_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=MASTER_COLUMNS)
    out = df.copy()
    if "source" not in out.columns and "index" in out.columns:
        out["source"] = out["index"].map(_source_from_index)
    for col in MASTER_COLUMNS:
        if col not in out.columns:
            out[col] = None
    return out[MASTER_COLUMNS].copy()


def _source_from_index(idx: Any) -> str:
    s = str(idx or "").strip()
    return s.split(":", 1)[0].lower() if ":" in s else ""


def _clean_value(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def load_table(csv_path: str = "master.csv") -> pd.DataFrame:
    """
    Load prebuilt master table from CSV (utf-8-sig).

    - Keeps master schema columns.
    - Restores ``params`` from serialized dict-like strings.
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df = _ensure_master_columns(df)

    def parse_params(v: Any) -> dict[str, Any]:
        if isinstance(v, dict):
            return v
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return {}
        s = str(v).strip()
        if not s:
            return {}
        try:
            parsed = ast.literal_eval(s)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, SyntaxError):
            return {}

    df["params"] = df["params"].map(parse_params)
    return df


def search_table(
    master: pd.DataFrame,
    *,
    query: Optional[str] = None,
    source: Optional[str] = None,
    cycle: Optional[str] = None,
    table_id: Optional[str] = None,
    params_key: Optional[str] = None,
    use_regex: bool = False,
    case_sensitive: bool = False,
    limit: Optional[int] = 100,
) -> pd.DataFrame:
    """
    Search master table with common filters.

    Parameters
    ----------
    master:
        Master table DataFrame (build_master_table/load_table result).
    query:
        Keyword to search across index/table_id/table_name.
    source:
        Source filter (e.g. kosis, ecos, imf, enara, molit, worldbank).
    cycle:
        Cycle filter (Y/M/Q/H/F).
    table_id:
        Exact table_id match.
    params_key:
        Keep rows whose params(dict) includes this key.
    use_regex:
        If True, query is treated as regex pattern.
    case_sensitive:
        String match case sensitivity.
    limit:
        Maximum rows to return. Use None for all.
    """
    if master is None or master.empty:
        return pd.DataFrame(columns=MASTER_COLUMNS)

    df = _ensure_master_columns(master)
    mask = pd.Series(True, index=df.index)

    if source:
        src = str(source).strip().lower()
        mask &= df["source"].astype(str).str.lower().eq(src)

    if cycle:
        cyc = str(cycle).strip().upper()
        mask &= df["cycle"].astype(str).str.upper().eq(cyc)

    if table_id:
        tid = str(table_id).strip()
        if case_sensitive:
            mask &= df["table_id"].astype(str).eq(tid)
        else:
            mask &= df["table_id"].astype(str).str.lower().eq(tid.lower())

    if params_key:
        pkey = str(params_key).strip()
        mask &= df["params"].map(
            lambda p: isinstance(p, dict) and (
                (pkey in p) if case_sensitive else (pkey.lower() in {str(k).lower() for k in p.keys()})
            )
        )

    if query:
        q = str(query)
        text_cols = (
            df["index"].fillna("").astype(str)
            + " "
            + df["table_id"].fillna("").astype(str)
            + " "
            + df["table_name"].fillna("").astype(str)
        )
        if use_regex:
            mask &= text_cols.str.contains(q, case=case_sensitive, regex=True, na=False)
        else:
            if case_sensitive:
                mask &= text_cols.str.contains(re.escape(q), regex=True, na=False)
            else:
                mask &= text_cols.str.lower().str.contains(q.lower(), regex=False, na=False)

    out = df[mask].copy()
    if limit is not None:
        out = out.head(int(limit))
    return out


def build_enara_master_table(*, timeout: int = 60) -> pd.DataFrame:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods={"GET"},
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/xml, application/xml, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    root: Optional[ET.Element] = None
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            resp = session.get(
                ENARA_XML_INDEX_URL,
                params={"userId": "apjh2529", "idntfcId": "229151DF51U114T0"},
                headers=headers,
                timeout=timeout,
            )
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            break
        except (requests.RequestException, ET.ParseError) as exc:
            last_error = exc
            # Connection resets are common on this endpoint; stagger retries.
            time.sleep(0.8 * attempt)
    if root is None:
        if last_error is not None:
            print(f"[enara] index fetch failed after retries: {last_error}")
        return _to_master_df([])

    rows: list[dict[str, Any]] = []
    for indicator in root.findall("지표"):
        indicator_code = str(indicator.findtext("지표코드") or "").strip()
        for table in indicator.findall("통계표"):
            table_id = str(table.findtext("통계표코드") or "").strip()
            table_name = str(table.findtext("통계표명") or "").strip()
            if not table_id or not table_name:
                continue
            rows.append(
                {
                    "index": f"enara:{indicator_code}:{table_id}",
                    "table_id": table_id,
                    "table_name": table_name,
                    "start_date": None,
                    "end_date": None,
                    "cycle": "Y",
                    "params": {
                        "stats_code": table_id,
                        "indicator_code": indicator_code or None,
                    },
                }
            )
    return _to_master_df(rows)


def _molit_stat_list_fragment(xml_text: str) -> str:
    soup = BeautifulSoup(xml_text, "html.parser")
    node = soup.find("statlist") or soup.find("statList")
    if node is None:
        return ""
    return (node.text or "").strip()


def _molit_fetch_groups(category_code: str, *, timeout: int) -> list[dict[str, str]]:
    try:
        resp = requests.post(
            MOLIT_PARTIAL_LIST_URL,
            params={
                "actionFlag": "mid",
                "statGb": "0310001",
                "codeCate": category_code,
                "iconFlag": "false",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    fragment = _molit_stat_list_fragment(resp.text)
    html = BeautifulSoup(fragment, "html.parser")
    groups: list[dict[str, str]] = []
    for a in html.find_all("a"):
        rsid = str(a.get("rsid") or "").strip()
        stat_gb = str(a.get("stat-gb") or "").strip() or "0310001"
        name = a.get_text(" ", strip=True)
        if rsid and name:
            groups.append({"rsid": rsid, "stat_gb": stat_gb, "name": name})
    return groups


def _molit_fetch_datasets(category_code: str, rsid: str, *, timeout: int) -> list[dict[str, str]]:
    try:
        resp = requests.post(
            MOLIT_PARTIAL_LIST_URL,
            params={
                "actionFlag": "statform",
                "statGb": "0310001",
                "codeCate": category_code,
                "codeDetail": rsid,
                "iconFlag": "true",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    fragment = _molit_stat_list_fragment(resp.text)
    html = BeautifulSoup(fragment, "html.parser")
    rows: list[dict[str, str]] = []
    for a in html.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        if not href:
            continue
        full = urljoin("https://stat.molit.go.kr", href)
        parsed = parse_qs(urlparse(full).query)
        form_id = (parsed.get("hFormId") or [""])[0].strip()
        rsid_value = (parsed.get("hRsId") or [rsid])[0].strip()
        name = a.get_text(" ", strip=True)
        if form_id and rsid_value and name:
            rows.append({"form_id": form_id, "rsid": rsid_value, "name": name, "view_url": full})
    return rows


def _molit_fetch_defaults(form_id: str, rsid: str, *, timeout: int) -> tuple[Optional[str], Optional[str], Optional[str]]:
    resp = requests.get(
        MOLIT_STAT_VIEW_URL,
        params={"hRsId": rsid, "hFormId": form_id, "hDivEng": "", "month_yn": ""},
        timeout=timeout,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def option_values(select_id: str) -> list[str]:
        sel = soup.find("select", id=select_id)
        if sel is None:
            return []
        out: list[str] = []
        for opt in sel.find_all("option"):
            value = str(opt.get("value") or opt.get_text(strip=True) or "").strip()
            if value:
                out.append(value)
        return out

    style_options = option_values("sStyleNum")
    start_options = option_values("sStart")
    end_options = option_values("sEnd")
    style = style_options[0] if style_options else None
    start_dt = start_options[-1] if start_options else None
    end_dt = end_options[0] if end_options else None
    return style, start_dt, end_dt


def _molit_chunk_ranges(start_dt: str, end_dt: str, *, chunk_years: int = 5) -> list[tuple[str, str]]:
    s = str(start_dt or "").strip()
    e = str(end_dt or "").strip()
    if not (s.isdigit() and e.isdigit()):
        return [(s, e)] if s and e else []
    if len(s) != len(e):
        return [(s, e)]

    # Year-only format: YYYY
    if len(s) == 4:
        sy, ey = int(s), int(e)
        ranges: list[tuple[str, str]] = []
        cur = sy
        while cur <= ey:
            chunk_end = min(cur + chunk_years - 1, ey)
            ranges.append((f"{cur:04d}", f"{chunk_end:04d}"))
            cur = chunk_end + 1
        return ranges

    # Monthly-like format: YYYYMM (keep month anchors on boundaries)
    if len(s) == 6:
        sy, sm = int(s[:4]), int(s[4:])
        ey, em = int(e[:4]), int(e[4:])
        ranges = []
        cur_y, cur_m = sy, sm
        while (cur_y, cur_m) <= (ey, em):
            end_y = min(cur_y + chunk_years - 1, ey)
            end_m = em if end_y == ey else 12
            ranges.append((f"{cur_y:04d}{cur_m:02d}", f"{end_y:04d}{end_m:02d}"))
            cur_y = end_y + 1
            cur_m = 1
        return ranges

    return [(s, e)]


def _fetch_molit_chunked(
    *,
    form_id: str,
    style_num: str,
    start_dt: str,
    end_dt: str,
) -> pd.DataFrame:
    chunks = _molit_chunk_ranges(start_dt, end_dt, chunk_years=5)
    if not chunks:
        return pd.DataFrame()
    parts: list[pd.DataFrame] = []
    skipped_406 = 0
    for sdt, edt in chunks:
        try:
            part = fetch_molit_open_api(
                form_id=form_id,
                style_num=style_num,
                start_dt=sdt,
                end_dt=edt,
            )
        except requests.HTTPError as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and resp.status_code == 406:
                skipped_406 += 1
                continue
            raise
        if not part.empty:
            parts.append(part)
    if not parts:
        if skipped_406 == len(chunks):
            # Some tables are served only by MOLIT public endpoint.
            public_parts: list[pd.DataFrame] = []
            for sdt, edt in chunks:
                try:
                    p = fetch_molit_public_stat(
                        form_id=form_id,
                        style_num=style_num,
                        start_dt=sdt,
                        end_dt=edt,
                    )
                except requests.RequestException:
                    continue
                if not p.empty:
                    public_parts.append(p)
            if public_parts:
                return pd.concat(public_parts, ignore_index=True).drop_duplicates()
            raise ValueError(
                "MOLIT returned 406 (Not Acceptable) for all chunked ranges, "
                "and public endpoint fallback returned no rows. "
                f"form_id={form_id}, style_num={style_num}, start_dt={start_dt}, end_dt={end_dt}"
            )
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).drop_duplicates()


def build_molit_master_table(*, timeout: int = 60) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for category_code, category_name in MOLIT_CATEGORY_CODES.items():
        groups = _molit_fetch_groups(category_code, timeout=timeout)
        for group in groups:
            datasets = _molit_fetch_datasets(category_code, group["rsid"], timeout=timeout)
            for ds in datasets:
                try:
                    style_num, start_dt, end_dt = _molit_fetch_defaults(
                        ds["form_id"],
                        ds["rsid"],
                        timeout=timeout,
                    )
                except requests.RequestException:
                    style_num, start_dt, end_dt = "1", None, None
                rows.append(
                    {
                        "index": f"molit:{ds['form_id']}",
                        "table_id": ds["form_id"],
                        "table_name": ds["name"],
                        "start_date": start_dt,
                        "end_date": end_dt,
                        "cycle": "Y",
                        "params": {
                            "form_id": ds["form_id"],
                            "style_num": style_num,
                            "start_dt": start_dt,
                            "end_dt": end_dt,
                            "category_code": category_code,
                            "category_name": category_name,
                            "group_name": group["name"],
                            "view_url": ds["view_url"],
                        },
                    }
                )
    return _to_master_df(rows)


def build_worldbank_master_table(*, timeout: int = 60) -> pd.DataFrame:
    page = 1
    total_pages = 1
    rows: list[dict[str, Any]] = []
    while page <= total_pages:
        resp = requests.get(
            WORLD_BANK_INDICATOR_URL,
            params={"format": "json", "per_page": 20000, "page": page},
            timeout=timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list) or len(payload) < 2:
            break
        meta, items = payload
        if isinstance(meta, dict):
            total_pages = int(meta.get("pages", 1))
        for item in items:
            indicator = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            if not indicator or not name:
                continue
            rows.append(
                {
                    "index": f"worldbank:{indicator}",
                    "table_id": indicator,
                    "table_name": name,
                    "cycle": "Y",
                    "start_date": None,
                    "end_date": None,
                    "params": {
                        "indicator": indicator,
                        "countries": None,
                        "start_year": None,
                        "end_year": None,
                    },
                }
            )
        page += 1
    return _to_master_df(rows)


def build_openfiscal_master_table(*, timeout: int = 60) -> pd.DataFrame:
    headers = {
        "AJAX": "true",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": "https://www.openfiscaldata.go.kr",
        "Referer": "https://www.openfiscaldata.go.kr/op/ko/sd/UOPKOSDA01",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    payload = {
        "opKoSdDtsStatsDVO": {
            "searchKeyword": "",
            "ofdMngOgNm": "",
            "ognSysNm": "",
            "rlsSvIxNm": "A",
        }
    }
    # Prefer latest available fiscal year as a sensible default query param.
    default_acnt_yr: Optional[str] = None
    try:
        yr_resp = requests.post(OPENFISCAL_YEAR_LIST_URL, headers=headers, json={}, timeout=timeout)
        yr_resp.raise_for_status()
        yr_json = yr_resp.json()
        years = [
            str(item.get("condCd")).strip()
            for item in yr_json.get("opKoCmYrList", [])
            if str(item.get("condCd") or "").strip()
        ]
        if years:
            default_acnt_yr = sorted(years)[-1]
    except Exception:  # noqa: BLE001
        default_acnt_yr = None

    resp = requests.post(OPENFISCAL_LIST_URL, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("selectScolSrchList", [])
    if not rows:
        return _to_master_df([])

    master_rows: list[dict[str, Any]] = []
    for row in rows:
        table_id = str(row.get("odtId") or "").strip()
        table_name = str(row.get("odtNm") or "").strip()
        if not table_id:
            continue
        master_rows.append(
            {
                "index": f"openfiscal:{table_id}",
                "table_id": table_id,
                "table_name": table_name or table_id,
                "cycle": None,
                "start_date": None,
                "end_date": None,
                "params": {
                    "service_id": table_id,
                    "extra_params": {"ACNT_YR": default_acnt_yr} if default_acnt_yr else {},
                    "dta_load_prd_nm": row.get("dtaLoadPrdNm"),
                    "all_dta_cls_nm": row.get("allDtaClsNm"),
                    "ofd_mng_og_cd": row.get("ofdMngOgCd"),
                    "ofd_mng_og_nm": row.get("ofdMngOgNm"),
                },
            }
        )
    return _to_master_df(master_rows)


def build_master_table(
    *,
    kosis_excel_path: str,
    include_sources: tuple[str, ...] = (
        "kosis",
        "ecos",
        "imf",
        "enara",
        "molit",
        "worldbank",
        "openfiscal",
    ),
    timeout: int = 60,
    show_progress: bool = False,
) -> pd.DataFrame:
    tables: list[pd.DataFrame] = []
    issues: list[str] = []
    for source in include_sources:
        source_l = source.lower().strip()
        source_bar = None
        if show_progress and tqdm is not None:
            source_bar = tqdm(total=1, desc=f"[{source_l}]", unit="task", leave=True)
        try:
            if source_l == "kosis":
                df_source = _ensure_master_columns(build_kosis_master_table(kosis_excel_path))
                tables.append(df_source)
            elif source_l == "ecos":
                df_source = _ensure_master_columns(build_ecos_master_table(timeout=timeout))
                tables.append(df_source)
            elif source_l == "imf":
                df_source = _ensure_master_columns(build_imf_master_table(timeout=timeout))
                tables.append(df_source)
            elif source_l == "enara":
                df_source = _ensure_master_columns(build_enara_master_table(timeout=timeout))
                if df_source.empty:
                    issues.append("enara: no rows collected (network/site response issue)")
                tables.append(df_source)
            elif source_l == "molit":
                df_source = _ensure_master_columns(build_molit_master_table(timeout=timeout))
                tables.append(df_source)
            elif source_l in {"worldbank", "world_bank"}:
                df_source = _ensure_master_columns(build_worldbank_master_table(timeout=timeout))
                tables.append(df_source)
            elif source_l == "openfiscal":
                df_source = _ensure_master_columns(build_openfiscal_master_table(timeout=timeout))
                tables.append(df_source)
            else:
                raise ValueError(f"Unsupported source: {source}")
            if source_bar is not None:
                source_bar.set_postfix(rows=len(df_source))
        except Exception as exc:  # noqa: BLE001
            issues.append(f"{source_l}: {exc}")
            if source_bar is not None:
                source_bar.set_postfix(error=str(exc)[:60])
        finally:
            if source_bar is not None:
                source_bar.update(1)
                source_bar.close()
        if show_progress and tqdm is None:
            print(f"[build_master_table] done: {source_l}")
    if not tables:
        out = pd.DataFrame(columns=MASTER_COLUMNS)
        out.attrs["build_issues"] = issues
        return out
    out = pd.concat(tables, ignore_index=True, sort=False)
    out = out[MASTER_COLUMNS].copy()
    out.attrs["build_issues"] = issues
    return out


def fetch_from_master_row(row: pd.Series, **overrides: Any) -> pd.DataFrame:
    source = _source_from_index(row.get("index"))
    if source == "kosis":
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        tbl_id = str(overrides.get("tbl_id") or row.get("table_id") or "").strip()
        cycle = str(overrides.get("cycle") or row.get("cycle") or "Y").upper()
        start_token = str(overrides.get("start_date") or row.get("start_date") or params.get("start_date") or "")[:4]
        end_token = str(overrides.get("end_date") or row.get("end_date") or params.get("end_date") or "")[:4]
        start_year = int(start_token) if start_token.isdigit() else 2000
        end_year = int(end_token) if end_token.isdigit() else pd.Timestamp.now().year
        if not tbl_id:
            raise ValueError("kosis row has no table_id(tbl_id).")
        try:
            return fetch_kosis_table(
                tbl_id=tbl_id,
                cycle=cycle,
                start_year=start_year,
                end_year=end_year,
                obj_filters=overrides.get("obj_filters"),
            )
        except TypeError as exc:
            # Newer KOSIS adapter may not expose obj_filters; keep master fetch compatible.
            if "obj_filters" not in str(exc):
                raise
            return fetch_kosis_table(
                tbl_id=tbl_id,
                cycle=cycle,
                start_year=start_year,
                end_year=end_year,
            )

    if source == "ecos":
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        stat_code = str(overrides.get("stat_code") or row.get("table_id") or "").strip()
        cycle = (
            _clean_value(overrides.get("cycle"))
            or _clean_value(row.get("cycle"))
            or _clean_value(params.get("cycle"))
            or "A"
        ).upper()
        start_time = (
            _clean_value(overrides.get("start_time"))
            or _clean_value(row.get("start_date"))
            or _clean_value(params.get("start_time"))
            or ("200001" if cycle == "M" else "2000")
        )
        end_time = (
            _clean_value(overrides.get("end_time"))
            or _clean_value(row.get("end_date"))
            or _clean_value(params.get("end_time"))
            or ("202412" if cycle == "M" else str(pd.Timestamp.now().year))
        )
        if not stat_code:
            raise ValueError("ecos row has no table_id(stat_code).")
        data = fetch_ecos_statistic_search(
            stat_code=stat_code,
            cycle=cycle,
            start_time=start_time,
            end_time=end_time,
        )
        # Some ECOS tables are labeled quarterly but served as annual(A).
        if data.empty and cycle == "Q":
            fallback_start = start_time[:4] if len(start_time) >= 4 else start_time
            fallback_end = end_time[:4] if len(end_time) >= 4 else end_time
            data = fetch_ecos_statistic_search(
                stat_code=stat_code,
                cycle="A",
                start_time=fallback_start,
                end_time=fallback_end,
            )
        return data

    if source == "imf":
        indicator = str(overrides.get("indicator") or row.get("table_id") or "").strip()
        start_year_v = overrides.get("start_year")
        end_year_v = overrides.get("end_year")
        if not indicator:
            raise ValueError("imf row has no table_id(indicator).")
        return fetch_imf_datamapper(
            indicator=indicator,
            countries=overrides.get("countries"),
            start_year=int(start_year_v) if start_year_v is not None else None,
            end_year=int(end_year_v) if end_year_v is not None else None,
        )

    if source == "enara":
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        stats_code = str(overrides.get("stats_code") or row.get("table_id") or "").strip()
        indicator_code = overrides.get("indicator_code") or params.get("indicator_code")
        if not stats_code:
            raise ValueError("enara row has no table_id(stats_code).")
        return fetch_enara_table(stats_code=stats_code, indicator_code=indicator_code)

    if source == "molit":
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        form_id = str(overrides.get("form_id") or row.get("table_id") or "").strip()
        style_num = overrides.get("style_num") or params.get("style_num") or "1"
        start_dt = overrides.get("start_dt") or row.get("start_date") or params.get("start_dt")
        end_dt = overrides.get("end_dt") or row.get("end_date") or params.get("end_dt")
        if not form_id:
            raise ValueError("molit row has no table_id(form_id).")
        if not start_dt or not end_dt:
            raise ValueError("molit row has no valid start_dt/end_dt.")
        return _fetch_molit_chunked(
            form_id=form_id,
            style_num=str(style_num),
            start_dt=str(start_dt),
            end_dt=str(end_dt),
        )

    if source in {"worldbank", "world_bank"}:
        try:
            from .world_bank import fetch_wb_indicator_panel
        except ImportError:
            from apifunction.world_bank import fetch_wb_indicator_panel
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        indicator = str(overrides.get("indicator") or row.get("table_id") or "").strip()
        if not indicator:
            raise ValueError("worldbank row has no table_id(indicator).")
        return fetch_wb_indicator_panel(
            indicator=indicator,
            countries=overrides.get("countries") or params.get("countries"),
            start_year=int(overrides.get("start_year") or params.get("start_year") or 2000),
            end_year=overrides.get("end_year") or params.get("end_year"),
        )

    if source == "openfiscal":
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        service_id = str(
            overrides.get("service_id") or params.get("service_id") or row.get("table_id") or ""
        ).strip()
        if not service_id:
            raise ValueError("openfiscal row has no table_id(service_id).")

        reserved = {"service_id", "api_key", "api_key_file", "page_size", "timeout", "extra_params"}
        extra_params = {}
        base_extra = params.get("extra_params")
        if isinstance(base_extra, dict):
            extra_params.update(base_extra)
        ov_extra = overrides.get("extra_params")
        if isinstance(ov_extra, dict):
            extra_params.update(ov_extra)
        for k, v in overrides.items():
            if k not in reserved and v is not None:
                extra_params[str(k)] = str(v)

        return fetch_openfiscal_service(
            service_id=service_id,
            api_key=overrides.get("api_key"),
            api_key_file=overrides.get("api_key_file"),
            extra_params=extra_params or None,
            page_size=int(overrides.get("page_size") or 1000),
            timeout=int(overrides.get("timeout") or 60),
        )

    raise ValueError(f"Unsupported source={source}")


def fetch_one(
    master: pd.DataFrame,
    *,
    dataset_id: str,
    source: Optional[str] = None,
    **overrides: Any,
) -> pd.DataFrame:
    key = str(dataset_id)
    key_upper = key.strip().upper()
    if source is not None:
        subset = master[
            master["index"].astype(str).str.lower().str.startswith(source.lower() + ":")
            & master["table_id"].astype(str).eq(key)
        ]
    else:
        subset = pd.DataFrame()
        if "index" in master.columns:
            subset = master[master["index"].astype(str).eq(key)]
        if subset.empty:
            subset = master[master["table_id"].astype(str).eq(key)]
        # Convenience: when given OPFI*** id without source, prefer openfiscal row.
        if subset.empty and key_upper.startswith("OPFI"):
            subset = master[
                master["index"].astype(str).str.lower().str.startswith("openfiscal:")
                & master["table_id"].astype(str).str.upper().eq(key_upper)
            ]

    if subset.empty:
        if source is None:
            raise ValueError(f"No row found for table_id/index={dataset_id}")
        raise ValueError(f"No row found for source={source}, table_id={dataset_id}")

    if source is None:
        matched_sources = sorted({_source_from_index(v) for v in subset["index"].tolist()})
        if len(matched_sources) > 1:
            raise ValueError(
                "Multiple sources matched this id. "
                f"Please specify source explicitly. matched_sources={matched_sources}"
            )
    return fetch_from_master_row(subset.iloc[0], **overrides)


@dataclass
class VerifyResult:
    source: str
    dataset_id: str
    ok: bool
    row_count: int
    error: str


def verify_source_fetch(master: pd.DataFrame, *, per_source: int = 1) -> pd.DataFrame:
    results: list[VerifyResult] = []
    probe = master.copy()
    probe["source"] = probe["index"].map(_source_from_index)
    grouped = probe.dropna(subset=["source", "table_id"]).groupby("source", sort=True)
    for source, group in grouped:
        success_count = 0
        tested = 0
        for _, row in group.iterrows():
            if success_count >= per_source:
                break
            if tested >= max(20, per_source * 5):
                break
            tested += 1
            dataset_id = str(row["table_id"])
            try:
                data = fetch_from_master_row(row)
                row_count = int(len(data))
                ok = row_count > 0
                err = "" if ok else "empty result"
                results.append(
                    VerifyResult(
                        source=str(source),
                        dataset_id=dataset_id,
                        ok=ok,
                        row_count=row_count,
                        error=err,
                    )
                )
                if ok:
                    success_count += 1
            except Exception as exc:  # noqa: BLE001
                results.append(
                    VerifyResult(
                        source=str(source),
                        dataset_id=dataset_id,
                        ok=False,
                        row_count=0,
                        error=str(exc),
                    )
                )
    return pd.DataFrame([r.__dict__ for r in results])


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build and verify a unified master table.")
    p.add_argument("--kosis-excel", required=True, help="Path to KOSIS excel catalog")
    p.add_argument(
        "--sources",
        default="kosis,ecos,imf,enara,molit,worldbank,openfiscal",
        help="Comma-separated sources: kosis,ecos,imf,enara,molit,worldbank,openfiscal",
    )
    p.add_argument("--output-csv", default="", help="Optional output CSV path")
    p.add_argument("--verify", action="store_true", help="Run per-source fetch smoke test")
    p.add_argument("--verify-per-source", type=int, default=1, help="Rows to verify by source")
    p.add_argument("--progress", action="store_true", help="Show build progress by source")
    return p


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    include_sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    master = build_master_table(
        kosis_excel_path=args.kosis_excel,
        include_sources=include_sources,
        show_progress=args.progress,
    )
    print(f"master rows={len(master):,}, cols={len(master.columns)}")
    if not master.empty:
        by_source = master["index"].map(_source_from_index).value_counts(dropna=False)
        print("rows by source:")
        print(by_source.to_string())
    issues = master.attrs.get("build_issues") or []
    if issues:
        print("\n[build_issues]")
        for issue in issues:
            print(f"- {issue}")
    if args.output_csv:
        master.to_csv(args.output_csv, index=False, encoding="utf-8-sig")
        print(f"saved: {args.output_csv}")

    if args.verify:
        verify_df = verify_source_fetch(master, per_source=args.verify_per_source)
        print("\n[verify]")
        if verify_df.empty:
            print("No rows to verify.")
        else:
            print(verify_df.to_string(index=False))


if __name__ == "__main__":
    main()
