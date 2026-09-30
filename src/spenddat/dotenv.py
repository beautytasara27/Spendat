"""Minimal ``.env`` loader (no dependency). Real environment variables always win.

Lines are ``KEY=value``; blank lines and ``#`` comments are ignored; surrounding quotes
are stripped. The file is git-ignored - use it for local development keys only.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env", environ: dict[str, str] | None = None) -> list[str]:
    env = environ if environ is not None else os.environ
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in env:
            env[key] = value
            loaded.append(key)
    return loaded
