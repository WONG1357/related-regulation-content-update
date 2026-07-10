from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .amendment_drafter import PROMPT_VERSION, amendment_drafting_agent, impact_review_agent
from .amendment_report_generator import generate_amendment_matrix, generate_amendment_report
from .critic_agent import critic_agent_review
from .document_cache import document_content_hash
from .document_editor import create_draft_document_copy


REQUIRED_EXCEL_SUMMARY_LIMITATION = (
    "This draft amendment suggestion is based on the Excel amendment summary, not on a verified clause-by-clause "
    "comparison of the old and latest regulation text. QA/RA confirmation is required before implementation."
)
REQUIRED_UNVERIFIED_CLAUSE_LIMITATION = (
    "The exact clause-level amendment could not be verified from official full-text regulation documents."
)
LOW_CONFIDENCE_LIMITATION = (
    "The evidence level is low. This output should be treated as a review prompt, not as a confirmed amendment."
)


def generate_agentic_amendment(
    selected_row: dict[str, Any] | pd.Series,
    document: dict[str, Any],
    output_dir: str | Path,
    regenerate: bool = False,
) -> dict[str, Any]:
    row = selected_row.to_dict() if isinstance(selected_row, pd.Series) else dict(selected_row)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cache_dir = output / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    context = build_amendment_context(row, document)
    cache_key = amendment_cache_key(row, document, context)
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.exists() and not regenerate:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if files_exist(cached):
            cached["loaded_from_cache"] = True
            return cached

    impact_review = impact_review_agent(context)
    critic_review = critic_agent_review(context, impact_review)
    drafting = amendment_drafting_agent(context, impact_review, critic_review)
    amendment_draft = build_amendment_draft(row, document, context, impact_review, critic_review, drafting, cache_key)

    editor_result = create_draft_document_copy(amendment_draft, output)
    amendment_draft["draft_file_path"] = editor_result.get("draft_file_path", "")
    amendment_draft["editor_warnings"] = editor_result.get("warnings", [])

    report_path = output / f"{amendment_draft['draft_id']}_amendment_report.docx"
    matrix_path = output / f"{amendment_draft['draft_id']}_amendment_matrix.xlsx"
    amendment_draft["report_file_path"] = str(generate_amendment_report(amendment_draft, report_path))
    amendment_draft["matrix_file_path"] = str(generate_amendment_matrix(amendment_draft, matrix_path))
    amendment_draft["loaded_from_cache"] = False
    cache_path.write_text(json.dumps(amendment_draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return amendment_draft


def build_amendment_context(row: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    selected_section = select_section(row, document)
    amendment_profile = {
        "source_mode": row.get("amendment_source_mode", ""),
        "confidence": row.get("amendment_confidence", ""),
        "amendment_summary_used": row.get("amendment_summary_used", ""),
        "exact_clause_verified": bool(row.get("exact_clause_verified", False)),
        "needs_human_confirmation": bool(row.get("needs_human_confirmation", True)),
        "limitations": parse_json_value(row.get("amendment_limitations", []), []),
        "affected_qms_topics": parse_json_value(row.get("affected_qms_topics", []), []),
        "search_keywords": parse_json_value(row.get("search_keywords", []), []),
    }
    return {
        "prompt_version": PROMPT_VERSION,
        "regulation_update": {
            "original_standard_code": row.get("regulation_old", ""),
            "latest_standard_code": row.get("regulation_latest", ""),
            "latest_year": row.get("latest_year", ""),
            "status": row.get("status", ""),
            "change_summary": row.get("change_summary", ""),
            "reference_source": row.get("reference_source", ""),
        },
        "amendment_profile": amendment_profile,
        "candidate_evidence": {
            "candidate_level": row.get("candidate_level", ""),
            "candidate_reason": row.get("candidate_reason", ""),
            "relationship_path": row.get("relationship_path", ""),
            "reference_evidence": row.get("reference_evidence", ""),
            "ai_decision": row.get("ai_decision", ""),
            "risk_level": row.get("risk_level", ""),
            "reason": row.get("reason", ""),
            "supporting_evidence": row.get("supporting_evidence", ""),
            "recommended_amendment_locations": parse_json_value(row.get("recommended_amendment_locations", []), []),
        },
        "document": {
            "document_code": document.get("document_code", ""),
            "document_title": document.get("document_title", ""),
            "document_rev": document.get("doc_revision", ""),
            "document_type": document.get("document_type", ""),
            "file_name": document.get("source_file_name") or document.get("file_name", ""),
            "source_file_path": document.get("source_file_path") or document.get("file_path", ""),
            "section_3_references": {
                "reference_regulations": document.get("reference_regulations", []),
                "reference_standards": document.get("reference_standards", []),
                "reference_guidance": document.get("reference_guidance", []),
                "reference_documents": document.get("reference_documents", []),
                "reference_forms": document.get("reference_forms", []),
            },
        },
        "selected_section": selected_section,
    }


def select_section(row: dict[str, Any], document: dict[str, Any]) -> dict[str, str]:
    sections = document.get("sections", []) or []
    locations = parse_json_value(row.get("recommended_amendment_locations", []), [])
    section_id = str(row.get("section_id", "") or first_location_value(locations, "section_id"))
    if section_id:
        for section in sections:
            if str(section.get("section_id", "")) == section_id:
                return section_payload(section)
    location_text = str(first_location_value(locations, "location") or row.get("section_location", ""))
    if location_text:
        for section in sections:
            if location_text.casefold() in str(section.get("page_or_location", "")).casefold():
                return section_payload(section)
    for section in sections:
        text = str(section.get("text", "")).strip()
        if text and len(text) > 30:
            return section_payload(section)
    return {"section_id": "", "section_title": "", "original_text": "", "location": ""}


def build_amendment_draft(
    row: dict[str, Any],
    document: dict[str, Any],
    context: dict[str, Any],
    impact_review: dict[str, Any],
    critic_review: dict[str, Any],
    drafting: dict[str, Any],
    cache_key: str,
) -> dict[str, Any]:
    final_decision = str(critic_review.get("final_decision") or impact_review.get("impact_decision") or "Need confirmation")
    confidence = str(impact_review.get("confidence") or row.get("amendment_confidence") or "Low")
    sections = drafting.get("affected_sections") or []
    if not sections:
        selected = context.get("selected_section", {})
        sections = [
            {
                "section_id": selected.get("section_id", ""),
                "section_title": selected.get("section_title", ""),
                "original_text": selected.get("original_text", ""),
                "proposed_text": "[DRAFT AMENDMENT - NOT APPROVED]\nQA/RA confirmation is required.",
                "change_type": "need_confirmation",
                "rationale": drafting.get("overall_rationale", ""),
                "evidence": [],
                "limitations": [],
            }
        ]
    limitations = required_limitations(row, confidence)
    for section in sections:
        section.setdefault("limitations", [])
        for limitation in limitations:
            if limitation not in section["limitations"]:
                section["limitations"].append(limitation)
    return {
        "draft_id": cache_key[:16],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "document_code": document.get("document_code", ""),
        "document_title": document.get("document_title", ""),
        "document_rev": document.get("doc_revision", ""),
        "source_file_path": document.get("source_file_path") or document.get("file_path", ""),
        "draft_file_path": "",
        "report_file_path": "",
        "regulation_id": row.get("regulation_old", ""),
        "original_standard_code": row.get("regulation_old", ""),
        "latest_standard_code": row.get("regulation_latest", ""),
        "amendment_source_mode": row.get("amendment_source_mode", ""),
        "amendment_summary_used": row.get("amendment_summary_used", ""),
        "exact_clause_verified": bool(row.get("exact_clause_verified", False)),
        "confidence": confidence,
        "impact_decision": final_decision,
        "affected_sections": sections,
        "critic_review": {
            "passed": bool(critic_review.get("passed", False)),
            "issues_found": critic_review.get("issues_found", []),
            "downgraded_decision": bool(critic_review.get("downgraded_decision", False)),
            "final_decision": final_decision,
            "reason": critic_review.get("reason", ""),
        },
        "limitations": limitations,
        "needs_human_confirmation": True,
        "qa_ra_review_status": "Draft only - not approved",
        "suggested_cr_ecr_action": drafting.get(
            "suggested_cr_ecr_action",
            "Open a controlled document-change request for QA/RA review if impact is confirmed.",
        ),
    }


def amendment_cache_key(row: dict[str, Any], document: dict[str, Any], context: dict[str, Any]) -> str:
    payload = {
        "document_code": document.get("document_code", ""),
        "document_file_hash": document_content_hash(document),
        "selected_section_id": context.get("selected_section", {}).get("section_id", ""),
        "amendment_profile_hash": stable_hash(context.get("amendment_profile", {})),
        "impact_decision_hash": stable_hash(
            {
                "ai_decision": row.get("ai_decision", ""),
                "reason": row.get("reason", ""),
                "supporting_evidence": row.get("supporting_evidence", ""),
            }
        ),
        "prompt_version": PROMPT_VERSION,
    }
    return stable_hash(payload)


def required_limitations(row: dict[str, Any], confidence: str) -> list[str]:
    limitations = parse_json_value(row.get("amendment_limitations", []), [])
    limitations = [str(item) for item in limitations if str(item).strip()]
    if row.get("amendment_source_mode") == "excel_amendment_summary":
        limitations.append(REQUIRED_EXCEL_SUMMARY_LIMITATION)
    if not bool(row.get("exact_clause_verified", False)):
        limitations.append(REQUIRED_UNVERIFIED_CLAUSE_LIMITATION)
    if str(confidence).casefold() == "low":
        limitations.append(LOW_CONFIDENCE_LIMITATION)
    return list(dict.fromkeys(limitations))


def parse_json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if value is None:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def first_location_value(locations: Any, key: str) -> str:
    if isinstance(locations, list) and locations:
        first = locations[0]
        if isinstance(first, dict):
            return str(first.get(key, ""))
    return ""


def section_payload(section: dict[str, Any]) -> dict[str, str]:
    text = str(section.get("text", ""))
    title = text.splitlines()[0][:120] if text else ""
    return {
        "section_id": str(section.get("section_id", "")),
        "section_title": title,
        "original_text": text,
        "location": str(section.get("page_or_location", "")),
    }


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def files_exist(amendment_draft: dict[str, Any]) -> bool:
    report = amendment_draft.get("report_file_path", "")
    matrix = amendment_draft.get("matrix_file_path", "")
    draft = amendment_draft.get("draft_file_path", "")
    required = [report, matrix]
    if draft:
        required.append(draft)
    return all(Path(path).exists() for path in required if path)
