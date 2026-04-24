from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _read_first_key_line(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    return None


def resolve_api_key(
    *,
    key_name: str,
    explicit_key: Optional[str] = None,
    explicit_file: Optional[str] = None,
    default_filename: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve API key in this order:
    1) explicit_key
    2) env var {KEY_NAME}_API_KEY
    3) explicit_file
    4) env var {KEY_NAME}_API_KEY_FILE
    5) apifunction/<default_filename>
    """
    if explicit_key and str(explicit_key).strip():
        return str(explicit_key).strip()

    upper = key_name.upper().strip()
    env_key = (os.getenv(f"{upper}_API_KEY") or "").strip()
    if env_key:
        return env_key

    base_dir = Path(__file__).resolve().parent
    candidates: list[Path] = []
    if explicit_file:
        candidates.append(Path(explicit_file).expanduser())

    env_file = (os.getenv(f"{upper}_API_KEY_FILE") or "").strip()
    if env_file:
        candidates.append(Path(env_file).expanduser())

    if default_filename:
        candidates.append(base_dir / default_filename)

    seen: set[str] = set()
    for c in candidates:
        p = c.expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        ps = str(p)
        if ps in seen:
            continue
        seen.add(ps)
        key = _read_first_key_line(p)
        if key:
            return key
    return None

