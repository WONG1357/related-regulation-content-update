from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from docx import Document
except ImportError:  # pragma: no cover
    Document = None


PARAGRAPH_SECTION_RE = re.compile(r"^P(?P<number>\d+)$")


def apply_amendment_to_source(
    amendment_draft: dict[str, Any],
    audit_dir: str | Path,
    row_id: str = "",
) -> dict[str, Any]:
    audit_root = Path(audit_dir)
    audit_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit = {
        "status": "failed",
        "row_id": row_id,
        "draft_id": amendment_draft.get("draft_id", ""),
        "source_file_path": amendment_draft.get("source_file_path", ""),
        "backup_file_path": "",
        "applied_sections": [],
        "error": "",
        "applied_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        result = _apply(amendment_draft, audit_root, stamp, audit)
    except Exception as exc:  # pragma: no cover - defensive audit path.
        audit["error"] = str(exc)
        result = audit
    audit_path = audit_root / f"{stamp}_{safe_name(str(audit.get('draft_id') or 'amendment'))}_apply_audit.json"
    result["audit_file_path"] = str(audit_path)
    audit_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _apply(amendment_draft: dict[str, Any], audit_root: Path, stamp: str, audit: dict[str, Any]) -> dict[str, Any]:
    if Document is None:
        audit["error"] = "python-docx is not installed."
        return audit
    source = Path(str(amendment_draft.get("source_file_path", "")))
    if not source.exists():
        audit["error"] = f"Source file not found: {source}"
        return audit
    if source.suffix.lower() != ".docx":
        audit["error"] = f"Only .docx source overwrite is supported. Got: {source.suffix}"
        return audit

    sections = amendment_draft.get("affected_sections", [])
    replacements = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("section_id", ""))
        match = PARAGRAPH_SECTION_RE.match(section_id)
        if not match:
            audit["error"] = f"Cannot safely apply non-paragraph section: {section_id}"
            return audit
        original_text = str(section.get("original_text", ""))
        proposed_text = str(section.get("proposed_text", ""))
        if not original_text.strip() or not proposed_text.strip():
            audit["error"] = f"Missing original/proposed text for section: {section_id}"
            return audit
        replacements.append((int(match.group("number")) - 1, section_id, original_text, proposed_text))

    if not replacements:
        audit["error"] = "No affected paragraph sections to apply."
        return audit

    doc = Document(source)
    seen_indexes = set()
    for paragraph_index, section_id, original_text, proposed_text in replacements:
        if paragraph_index in seen_indexes:
            audit["error"] = f"Duplicate target paragraph section: {section_id}"
            return audit
        seen_indexes.add(paragraph_index)
        if paragraph_index < 0 or paragraph_index >= len(doc.paragraphs):
            audit["error"] = f"Target paragraph not found for section: {section_id}"
            return audit
        current_text = doc.paragraphs[paragraph_index].text
        if current_text.strip() != original_text.strip():
            audit["error"] = f"Current source text does not match expected original text for section: {section_id}"
            audit["mismatch"] = {
                "section_id": section_id,
                "expected_original_text": original_text,
                "current_source_text": current_text,
            }
            return audit

    backup = audit_root / f"{source.stem}_backup_before_ai_apply_{stamp}{source.suffix}"
    shutil.copy2(source, backup)
    for paragraph_index, section_id, original_text, proposed_text in replacements:
        replace_paragraph_text(doc.paragraphs[paragraph_index], proposed_text)
        audit["applied_sections"].append(
            {
                "section_id": section_id,
                "before_text": original_text,
                "after_text": proposed_text,
            }
        )
    doc.save(source)
    audit["status"] = "applied"
    audit["backup_file_path"] = str(backup)
    return audit


def replace_paragraph_text(paragraph: Any, text: str) -> None:
    for run in paragraph.runs:
        run.text = ""
    if paragraph.runs:
        paragraph.runs[0].text = text
    else:
        paragraph.add_run(text)


def safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip())
    return cleaned.strip("_") or "amendment"
