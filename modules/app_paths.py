from __future__ import annotations

import os
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]


def _configured_path(variable: str, fallback: Path) -> Path:
    configured = os.getenv(variable, "").strip()
    return Path(configured).expanduser() if configured else fallback


def data_root() -> Path:
    return _configured_path("REGULATORY_APP_DATA_DIR", APP_ROOT / "data")


def original_file_root() -> Path:
    return _configured_path("REGULATORY_ORIGINAL_FILE_DIR", APP_ROOT.parent / "original file")


def environment_paths() -> list[Path]:
    configured = os.getenv("REGULATORY_APP_CONFIG_DIR", "").strip()
    roots = [Path(configured).expanduser()] if configured else []
    roots.append(APP_ROOT)
    return [root / name for root in roots for name in (".env", ".env.txt")]
