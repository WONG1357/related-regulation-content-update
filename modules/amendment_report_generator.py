from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

try:
    from docx import Document
except ImportError:  # pragma: no cover
    Document = None


def generate_amendment_report(amendment_draft: dict[str, Any], output_path: str | Path) -> Path:
    if Document is None:
        raise RuntimeError("python-docx is not installed.")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("Draft Amendment Report", 0)
    doc.add_paragraph("Draft only - not approved. Human QA/RA confirmation required.")

    add_fields(
        doc,
        "1. Document Information",
        [
            ("Document code", amendment_draft.get("document_code", "")),
            ("Document title", amendment_draft.get("document_title", "")),
            ("Document revision", amendment_draft.get("document_rev", "")),
            ("Source file", amendment_draft.get("source_file_path", "")),
            ("Draft file", amendment_draft.get("draft_file_path", "")),
        ],
    )
    add_fields(
        doc,
        "2. Regulation / Standard Update Information",
        [
            ("Original standard", amendment_draft.get("original_standard_code", "")),
            ("Latest standard", amendment_draft.get("latest_standard_code", "")),
            ("Regulation id", amendment_draft.get("regulation_id", "")),
            ("Summary used", amendment_draft.get("amendment_summary_used", "")),
        ],
    )
    add_fields(
        doc,
        "3. Amendment Source and Confidence",
        [
            ("Source mode", amendment_draft.get("amendment_source_mode", "")),
            ("Confidence", amendment_draft.get("confidence", "")),
            ("Exact clause verified", amendment_draft.get("exact_clause_verified", False)),
            ("Impact decision", amendment_draft.get("impact_decision", "")),
            ("QA/RA review status", amendment_draft.get("qa_ra_review_status", "")),
        ],
    )

    doc.add_heading("4. Affected Section", level=1)
    for section in amendment_draft.get("affected_sections", []):
        doc.add_heading(str(section.get("section_id", "Section")), level=2)
        add_paragraph_block(doc, "Section title", section.get("section_title", ""))
        add_paragraph_block(doc, "Original Text", section.get("original_text", ""))
        add_paragraph_block(doc, "Proposed Draft Amendment", section.get("proposed_text", ""))
        add_paragraph_block(doc, "Rationale", section.get("rationale", ""))
        add_paragraph_block(doc, "Evidence Used", "\n".join(str(item) for item in section.get("evidence", [])))
        add_paragraph_block(doc, "Limitations", "\n".join(str(item) for item in section.get("limitations", [])))

    add_paragraph_block(doc, "5. Critic Agent Review", amendment_draft.get("critic_review", {}))
    add_paragraph_block(doc, "6. Limitations", "\n".join(str(item) for item in amendment_draft.get("limitations", [])))
    add_paragraph_block(doc, "7. Suggested CR/ECR Action", amendment_draft.get("suggested_cr_ecr_action", ""))

    doc.add_heading("8. QA/RA Review and Approval Placeholder", level=1)
    for line in [
        "Reviewer name: ______________________________",
        "Reviewer decision: [ ] Accept draft for CR/ECR  [ ] Revise  [ ] Reject  [ ] Need more evidence",
        "Reviewer notes: ______________________________",
        "Approval date: ______________________________",
    ]:
        doc.add_paragraph(line)
    doc.save(path)
    return path


def generate_amendment_matrix(amendment_draft: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for section in amendment_draft.get("affected_sections", []):
        rows.append(
            {
                "draft_id": amendment_draft.get("draft_id", ""),
                "created_at": amendment_draft.get("created_at", ""),
                "document_code": amendment_draft.get("document_code", ""),
                "document_title": amendment_draft.get("document_title", ""),
                "document_rev": amendment_draft.get("document_rev", ""),
                "original_standard_code": amendment_draft.get("original_standard_code", ""),
                "latest_standard_code": amendment_draft.get("latest_standard_code", ""),
                "amendment_source_mode": amendment_draft.get("amendment_source_mode", ""),
                "confidence": amendment_draft.get("confidence", ""),
                "impact_decision": amendment_draft.get("impact_decision", ""),
                "exact_clause_verified": amendment_draft.get("exact_clause_verified", False),
                "section_id": section.get("section_id", ""),
                "section_title": section.get("section_title", ""),
                "change_type": section.get("change_type", ""),
                "rationale": section.get("rationale", ""),
                "needs_human_confirmation": amendment_draft.get("needs_human_confirmation", True),
                "qa_ra_review_status": amendment_draft.get("qa_ra_review_status", ""),
            }
        )
    pd.DataFrame(rows).to_excel(path, index=False)
    return path


def add_fields(doc: Any, heading: str, fields: list[tuple[str, Any]]) -> None:
    doc.add_heading(heading, level=1)
    for label, value in fields:
        doc.add_paragraph(f"{label}: {value}")


def add_paragraph_block(doc: Any, heading: str, value: Any) -> None:
    doc.add_heading(heading, level=1)
    doc.add_paragraph(str(value or ""))
