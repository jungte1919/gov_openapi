"""
DataFrame -> RAG용 Markdown + 청킹 JSONL 동시 생성.

범용 프로파일링(요약 통계, 결측, 상위 빈도, 표본 행)을 Markdown으로 만들고,
동일 내용을 청크 단위 JSONL로 저장해 임베딩/RAG 파이프라인에 바로 넣을 수 있게 합니다.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import numpy as np
import pandas as pd


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _truncate(s: str, max_len: int = 200) -> str:
    s = str(s).replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "..."


def _df_to_markdown_table(
    df: pd.DataFrame, max_rows: int = 15, max_cell: int = 80
) -> str:
    if df.empty:
        return "_표 데이터 없음_\n"
    show = df.head(max_rows).copy()
    for c in show.columns:
        if show[c].dtype == object or str(show[c].dtype) == "string":
            show[c] = show[c].map(lambda x: _truncate(x, max_cell))
    try:
        return show.to_markdown(index=False)
    except (ImportError, ValueError):
        return _dataframe_to_markdown_simple(show)


def _dataframe_to_markdown_simple(df: pd.DataFrame) -> str:
    """tabulate 미설치 시 사용하는 최소 Markdown 표."""
    cols = [str(c) for c in df.columns]
    esc = lambda x: str(x).replace("|", "\\|").replace("\n", " ")
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines = [header, sep]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(esc(row[c]) for c in df.columns) + " |")
    return "\n".join(lines)


@dataclass
class RagExportConfig:
    """Markdown + JSONL 내보내기 옵션."""

    chunk_chars: int = 1800
    chunk_overlap: int = 200
    sample_rows: int = 12
    max_categorical_levels: int = 12
    max_freq_terms: int = 8
    profile_rows_per_subtable: int = 22


@dataclass
class DatasetMeta:
    """데이터 출처·검색용 메타데이터."""

    slug: str
    title: str
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def build_analysis_markdown(
    df: pd.DataFrame,
    meta: DatasetMeta,
    *,
    cfg: Optional[RagExportConfig] = None,
) -> str:
    """
    DataFrame을 LLM/RAG에 적합한 분석 Markdown으로 변환합니다.
    """
    cfg = cfg or RagExportConfig()
    lines: list[str] = []

    lines.append(f"# {meta.title}")
    lines.append("")
    lines.append(f"- **생성 시각(UTC)**: {_utc_now_iso()}")
    if meta.source:
        lines.append(f"- **출처/소스**: {meta.source}")
    for k, v in meta.extra.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")

    lines.append("## 개요")
    lines.append("")
    if df.empty:
        lines.append("데이터프레임이 비어 있습니다.")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"- **행 수**: {len(df):,}")
    lines.append(f"- **열 수**: {len(df.columns)}")
    lines.append("")

    lines.append("## 열 목록")
    lines.append("")
    lines.append(", ".join(f"`{c}`" for c in df.columns))
    lines.append("")

    prof_rows: list[dict[str, Any]] = []
    for col in df.columns:
        s = df[col]
        nnull = int(s.isna().sum())
        nunq = int(s.nunique(dropna=True))
        prof_rows.append(
            {
                "열": col,
                "dtype": str(s.dtype),
                "비결측": int(len(s) - nnull),
                "결측": nnull,
                "고유값_수": nunq,
            }
        )
    prof = pd.DataFrame(prof_rows)
    step = max(1, int(cfg.profile_rows_per_subtable))
    if len(prof) <= step:
        lines.append("## 열 프로파일")
        lines.append("")
        lines.append(_df_to_markdown_table(prof, max_rows=len(prof), max_cell=120))
        lines.append("")
    else:
        for i in range(0, len(prof), step):
            sub = prof.iloc[i : i + step]
            hi = i + len(sub)
            lines.append(f"## 열 프로파일 ({i + 1}-{hi} / {len(prof)})")
            lines.append("")
            lines.append(_df_to_markdown_table(sub, max_rows=len(sub), max_cell=120))
            lines.append("")

    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if num_cols:
        lines.append("## 수치 요약 (describe)")
        lines.append("")
        desc = df[num_cols].describe().T
        lines.append(_df_to_markdown_table(desc.reset_index().rename(columns={"index": "열"})))
        lines.append("")

    obj_cols = [
        c
        for c in df.columns
        if df[c].dtype == object
        or str(df[c].dtype) in ("string", "category")
        or isinstance(df[c].dtype, pd.CategoricalDtype)
    ]
    for col in obj_cols[: cfg.max_categorical_levels]:
        vc = df[col].astype(str).value_counts().head(cfg.max_freq_terms)
        if len(vc) == 0:
            continue
        lines.append(f"## 범주/문자열 상위 빈도: `{col}`")
        lines.append("")
        freq_df = vc.reset_index()
        freq_df.columns = ["값", "건수"]
        lines.append(_df_to_markdown_table(freq_df))
        lines.append("")

    lines.append("## 표본 행")
    lines.append("")
    lines.append(_df_to_markdown_table(df.head(cfg.sample_rows)))
    lines.append("")

    lines.append("## 검색 힌트")
    lines.append("")
    lines.append(
        "이 문서는 통계/API 결과를 요약한 것입니다. 질의 시 열 이름·단위·기간(해당 시)을 "
        "함께 확인하세요."
    )
    lines.append("")

    return "\n".join(lines)


_HEADER_SPLIT = re.compile(r"(?=^## )", re.MULTILINE)


def _split_oversized_block(block: str, chunk_chars: int, overlap: int) -> list[str]:
    """
    긴 Markdown 블록을 줄 단위로 묶어 청크로 나눕니다(표 행이 중간에 잘리지 않도록).
    한 줄이 chunk_chars보다 길 때만 문자 슬라이딩을 사용합니다.
    """
    block = block.strip()
    if not block:
        return []
    if len(block) <= chunk_chars:
        return [block]

    lines = block.splitlines()
    chunks: list[str] = []
    cur_lines: list[str] = []

    def _flush() -> None:
        nonlocal cur_lines
        if cur_lines:
            chunks.append("\n".join(cur_lines))
            cur_lines = []

    def _hard_split_line(line: str) -> None:
        start = 0
        n = len(line)
        while start < n:
            end = min(n, start + chunk_chars)
            chunks.append(line[start:end])
            if end >= n:
                break
            start = max(0, end - overlap)

    for line in lines:
        if len(line) > chunk_chars:
            _flush()
            _hard_split_line(line)
            continue
        trial = "\n".join(cur_lines + [line]) if cur_lines else line
        if len(trial) <= chunk_chars:
            cur_lines.append(line)
        else:
            _flush()
            cur_lines = [line]
    _flush()
    return chunks


def split_markdown_into_chunks(
    markdown: str,
    *,
    chunk_chars: int = 1800,
    overlap: int = 200,
) -> list[str]:
    """
    Markdown을 RAG용 텍스트 청크로 나눕니다.

    1) `## ` 섹션 경계를 우선 존중합니다.
    2) 섹션이 길면 **줄(표 행) 단위**로 패킹한 뒤 청크 크기에 맞춥니다.
    """
    text = markdown.strip()
    if not text:
        return []

    sections: list[str] = []
    for p in _HEADER_SPLIT.split(text):
        p = p.strip()
        if not p:
            continue
        sections.extend(_split_oversized_block(p, chunk_chars, overlap))

    merged: list[str] = []
    buf = ""
    for s in sections:
        if not buf:
            buf = s
            continue
        if len(buf) + 2 + len(s) <= chunk_chars:
            buf = f"{buf}\n\n{s}"
        else:
            merged.append(buf)
            buf = s
    if buf:
        merged.append(buf)
    return merged


def chunks_to_rag_jsonl(
    chunks: Iterable[str],
    meta: DatasetMeta,
    out_path: Path | str,
    *,
    base_metadata: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """
    청크를 JSONL로 저장합니다. 각 줄은 임베딩 입력용 `text`와 메타데이터를 포함합니다.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base = dict(base_metadata or {})
    records: list[dict[str, Any]] = []
    with out_path.open("w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks):
            rec = {
                "chunk_id": f"{meta.slug}_{i:04d}",
                "dataset_slug": meta.slug,
                "dataset_title": meta.title,
                "source": meta.source,
                "chunk_index": i,
                "text": chunk,
                "metadata": {
                    **base,
                    "generated_at_utc": _utc_now_iso(),
                    "extra": meta.extra,
                },
            }
            records.append(rec)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return records


def export_rag_bundle(
    df: pd.DataFrame,
    meta: DatasetMeta,
    out_dir: Path | str,
    *,
    cfg: Optional[RagExportConfig] = None,
    base_metadata: Optional[Mapping[str, Any]] = None,
) -> tuple[Path, Path]:
    """
    동일 분석에 대해 `.md`와 청킹 `.jsonl`을 함께 저장합니다.

    Returns
    -------
    (markdown_path, jsonl_path)
    """
    cfg = cfg or RagExportConfig()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    md = build_analysis_markdown(df, meta, cfg=cfg)
    md_path = out_dir / f"{meta.slug}_rag.md"
    md_path.write_text(md, encoding="utf-8")

    chunks = split_markdown_into_chunks(
        md, chunk_chars=cfg.chunk_chars, overlap=cfg.chunk_overlap
    )
    jsonl_path = out_dir / f"{meta.slug}_rag_chunks.jsonl"
    chunks_to_rag_jsonl(chunks, meta, jsonl_path, base_metadata=base_metadata)
    return md_path, jsonl_path


def _safe_fetch(name: str, fn: Any) -> tuple[pd.DataFrame, Optional[str]]:
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 — 샘플 러너용
        return pd.DataFrame(), f"{type(exc).__name__}: {exc}"


def run_sample_exports(output_root: Optional[Path] = None) -> list[dict[str, Any]]:
    """
    apifunction 폴더의 API로 샘플 데이터를 가져와 `datatollm_sample_output`에 저장합니다.
    키가 없거나 네트워크 오류인 경우 해당 항목은 비어 있거나 에러 메시지가 기록됩니다.
    """
    from apifunction.ecos import fetch_ecos_statistic_search
    from apifunction.kosis import fetch_kosis_table
    from apifunction.molit import (
        BUILDING_STATS_FORM_ID,
        BUILDING_STATS_STYLE_NUM,
        fetch_molit_building_permit_stats,
        fetch_molit_public_columns,
        normalize_molit_column_names,
    )
    from apifunction.openfiscal import fetch_openfiscal_service

    root = Path(__file__).resolve().parent
    out = output_root or (root / "datatollm_sample_output")
    out.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []

    # --- MOLIT (키 불필요, public) ---
    df_m, err_m = _safe_fetch(
        "molit",
        lambda: normalize_molit_column_names(
            fetch_molit_building_permit_stats(start_dt="202301", end_dt="202312"),
            fetch_molit_public_columns(
                form_id=BUILDING_STATS_FORM_ID, style_num=BUILDING_STATS_STYLE_NUM
            ),
        ),
    )
    md_m, jl_m = export_rag_bundle(
        df_m,
        DatasetMeta(
            slug="molit_building_permit",
            title="MOLIT 건축허가·착공·준공통계 (표본)",
            source="MOLIT stat.molit.go.kr (public stat)",
            extra={
                "form_id": BUILDING_STATS_FORM_ID,
                "style_num": BUILDING_STATS_STYLE_NUM,
                "기간": "202301-202312",
                "fetch_error": err_m or "",
            },
        ),
        out / "molit_building_permit",
        base_metadata={"api": "molit", "table": "building_permit_public"},
    )
    summary.append(
        {
            "name": "molit_building_permit",
            "rows": len(df_m),
            "markdown": str(md_m),
            "jsonl": str(jl_m),
            "error": err_m,
        }
    )

    # --- OpenFiscal OPFI152 ---
    df_o, err_o = _safe_fetch(
        "openfiscal",
        lambda: fetch_openfiscal_service("OPFI152", page_size=500),
    )
    md_o, jl_o = export_rag_bundle(
        df_o,
        DatasetMeta(
            slug="openfiscal_OPFI152",
            title="OpenFiscal OPFI152 (표본)",
            source="openapi.openfiscaldata.go.kr / OPFI152",
            extra={"service_id": "OPFI152", "fetch_error": err_o or ""},
        ),
        out / "openfiscal_OPFI152",
        base_metadata={"api": "openfiscal", "service_id": "OPFI152"},
    )
    summary.append(
        {
            "name": "openfiscal_OPFI152",
            "rows": len(df_o),
            "markdown": str(md_o),
            "jsonl": str(jl_o),
            "error": err_o,
        }
    )

    # --- ECOS 200Y101 연간 일부 ---
    df_e, err_e = _safe_fetch(
        "ecos",
        lambda: fetch_ecos_statistic_search(
            "200Y101",
            cycle="A",
            start_time="2019",
            end_time="2023",
            item_code="?",
        ),
    )
    md_e, jl_e = export_rag_bundle(
        df_e,
        DatasetMeta(
            slug="ecos_200Y101",
            title="ECOS 200Y101 주요지표 연간 (표본)",
            source="ecos.bok.or.kr StatisticSearch",
            extra={
                "stat_code": "200Y101",
                "cycle": "A",
                "start_time": "2019",
                "end_time": "2023",
                "fetch_error": err_e or "",
            },
        ),
        out / "ecos_200Y101",
        base_metadata={"api": "ecos", "stat_code": "200Y101"},
    )
    summary.append(
        {
            "name": "ecos_200Y101",
            "rows": len(df_e),
            "markdown": str(md_e),
            "jsonl": str(jl_e),
            "error": err_e,
        }
    )

    # --- KOSIS DT_1YL20631 소량 연도 ---
    df_k, err_k = _safe_fetch(
        "kosis",
        lambda: fetch_kosis_table(
            "DT_1YL20631",
            cycle="A",
            start_year=2020,
            end_year=2022,
        ),
    )
    md_k, jl_k = export_rag_bundle(
        df_k,
        DatasetMeta(
            slug="kosis_DT_1YL20631",
            title="KOSIS DT_1YL20631 연령별 인구 (표본 연도)",
            source="kosis.kr Open API",
            extra={
                "tbl_id": "DT_1YL20631",
                "cycle": "A",
                "연도": "2020-2022",
                "fetch_error": err_k or "",
            },
        ),
        out / "kosis_DT_1YL20631",
        base_metadata={"api": "kosis", "tbl_id": "DT_1YL20631"},
    )
    summary.append(
        {
            "name": "kosis_DT_1YL20631",
            "rows": len(df_k),
            "markdown": str(md_k),
            "jsonl": str(jl_k),
            "error": err_k,
        }
    )

    index_path = out / "sample_run_summary.json"
    index_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="DataFrame -> RAG Markdown + chunked JSONL")
    parser.add_argument(
        "--skip-sample",
        action="store_true",
        help="네트워크 샘플 내보내기를 건너뜁니다(라이브러리만 사용할 때).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="샘플 출력 루트 (기본: apifunction/datatollm_sample_output)",
    )
    args = parser.parse_args()
    if not args.skip_sample:
        out = args.out or (Path(__file__).resolve().parent / "datatollm_sample_output")
        run_sample_exports(output_root=out)
        print("Sample exports written under", out)


if __name__ == "__main__":
    import sys

    _repo = Path(__file__).resolve().parent.parent
    if str(_repo) not in sys.path:
        sys.path.insert(0, str(_repo))
    main()
