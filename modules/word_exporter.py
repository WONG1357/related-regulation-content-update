from __future__ import annotations

from pathlib import Path
from typing import Any


def export_draft_revised_documents(*_: Any, **__: Any) -> dict[str, str]:
    return {
        "status": "skipped",
        "reason": (
            "MVP does not automatically modify controlled documents. "
            "Use the amendment report for QA/RA review."
        ),
    }


def draft_name(original_filename: str) -> str:
    return f"DRAFT_FOR_REVIEW_{Path(original_filename).name}"
