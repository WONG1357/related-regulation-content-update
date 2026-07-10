from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from docx import Document
except ImportError:  # pragma: no cover
    Document = None


def create_draft_document_copy(amendment_draft: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    source = Path(str(amendment_draft.get("source_file_path", "")))
    if not source.exists():
        return {"draft_file_path": "", "warnings": [f"Source file not found: {source}"]}
    if Document is None:
        return {"draft_file_path": "", "warnings": ["python-docx is not installed."]}

    suffix = source.suffix.lower()
    if suffix in {".xls", ".xlsx"}:
        return {"draft_file_path": "", "warnings": ["Spreadsheet source detected; generated report only."]}
    if suffix not in {".doc", ".docx"}:
        return {"draft_file_path": "", "warnings": [f"Unsupported draft-copy source type: {source.suffix}"]}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    document_code = safe_name(str(amendment_draft.get("document_code", "document")))
    rev = safe_name(str(amendment_draft.get("document_rev", "") or "rev"))
    draft_path = output / f"{document_code}_{rev}_draft_amendment_{stamp}.docx"
    if source.resolve() == draft_path.resolve():
        return {"draft_file_path": "", "warnings": ["Draft output path resolved to the original source path."]}

    working_path = draft_path
    warnings: list[str] = []
    if suffix == ".docx":
        shutil.copy2(source, working_path)
    else:
        converted = convert_doc_to_docx(source, working_path)
        if not converted:
            return {"draft_file_path": "", "warnings": ["Could not convert .doc source to .docx; generated report only."]}

    doc = Document(working_path)
    doc.add_page_break()
    doc.add_heading("[DRAFT AMENDMENT - NOT APPROVED]", level=1)
    doc.add_paragraph("This draft copy is generated for QA/RA review only. It is not an approved controlled document.")
    for section in amendment_draft.get("affected_sections", []):
        doc.add_heading(f"Proposed revision for Section {section.get('section_id', '')}", level=2)
        doc.add_paragraph(str(section.get("section_title", "")))
        doc.add_paragraph("Original text:")
        doc.add_paragraph(str(section.get("original_text", "")))
        doc.add_paragraph("Proposed draft text:")
        doc.add_paragraph(str(section.get("proposed_text", "")))
        doc.add_paragraph(f"Rationale: {section.get('rationale', '')}")
    doc.add_paragraph("Human QA/RA confirmation required before implementation.")
    doc.save(working_path)
    return {"draft_file_path": str(working_path), "warnings": warnings}


def convert_doc_to_docx(source: Path, target: Path) -> bool:
    textutil = shutil.which("textutil")
    if not textutil:
        return False
    result = subprocess.run(
        [textutil, "-convert", "docx", "-output", str(target), str(source)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    return result.returncode == 0 and target.exists() and target.stat().st_size > 0


def safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value.strip())
    return cleaned.strip("_") or "document"
