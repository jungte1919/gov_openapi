from __future__ import annotations

import io
from pathlib import Path
from typing import Any, BinaryIO, Union

import pandas as pd

Source = Union[str, Path, bytes, bytearray, BinaryIO]


def peek_excel_head(source: Source, n: int = 16) -> bytes:
    """Return first ``n`` bytes; for streams, restores the previous read position."""
    if isinstance(source, (bytes, bytearray)):
        return bytes(source[:n])
    if isinstance(source, (str, Path)):
        with open(source, "rb") as f:
            return f.read(n)
    if not hasattr(source, "read"):
        raise TypeError(f"unsupported source type: {type(source)!r}")
    pos = source.tell()
    try:
        chunk = source.read(n)
    finally:
        source.seek(pos)
    if isinstance(chunk, str):
        raise TypeError("binary stream required")
    return chunk if isinstance(chunk, bytes) else bytes(chunk)


def excel_engine_from_signature(head: bytes) -> str:
    """
    Choose a pandas ``engine`` from leading bytes.

    - ``PK`` zip header → Office Open XML (``.xlsx`` / ``.xlsm``) → ``openpyxl``
    - OLE compound header → legacy ``.xls`` → ``xlrd``
    """
    if len(head) < 4:
        raise ValueError("file is empty or too short to detect Excel format")

    if head[:2] == b"PK":
        return "openpyxl"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xlrd"

    stripped = head.lstrip(b"\xef\xbb\xbf")
    if stripped[:1] in (b"<",) or stripped.startswith(b"<!"):
        raise ValueError(
            "response looks like HTML/XML, not an Excel zip/binary file — "
            "check the URL (login page, 403, or wrong content-type) or file path."
        )

    raise ValueError(
        f"unrecognized binary signature {head[:8]!r}; not a normal .xlsx/.xls or file is corrupt."
    )


def read_excel_auto(source: Source, **kwargs: Any) -> pd.DataFrame:
    """
    ``pandas.read_excel`` with ``engine`` chosen from file magic.

    Avoids ``BadZipFile`` when a ``.xls`` is opened with the default xlsx engine,
    or when the extension lies about the real format.
    """
    head = peek_excel_head(source)
    engine = excel_engine_from_signature(head)
    kw = dict(kwargs)
    kw.setdefault("engine", engine)

    if isinstance(source, (bytes, bytearray)):
        return pd.read_excel(io.BytesIO(source), **kw)
    return pd.read_excel(source, **kw)
