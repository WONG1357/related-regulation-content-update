from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .ai_document_agent import ai_json


MAX_SECTION_CHARS = 4000
MAX_SECTIONS_PER_DOCUMENT = 6
MAX_CHANGES_PER_DOCUMENT = 16

ENGLISH_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9/-]{3,}\b")
CHINESE_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
GENERIC_TERMS = {
    "applicable",
    "application",
    "control",
    "device",
    "document",
    "general",
    "international",
    "management",
    "medical",
    "organization",
    "procedure",
    "process",
    "product",
    "quality",
    "requirement",
    "requirements",
    "standard",
    "system",
    "文件",
    "标准",
    "管理",
    "程序",
    "要求",
    "质量",
    "医疗器械",
}
EXCLUDED_SECTION_HEADINGS = (
    "revision history",
    "change history",
    "document history",
    "reference document",
    "references document",
    "參考文件",
    "参考文件",
)


@dataclass(frozen=True)
class RankedSection:
    section_id: str
    location: str
    text: str
    score: int
    matched_terms: list[str]
    evidence_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AmendmentSuggestion:
    document_code: str
    document_title: str
    file_path: str
    depth: int
    seed_code: str
    relationship_path: str
    decision: str
    confidence: str
    reason: str
    suggestions: list[dict[str, Any]]
    limitations: list[str]
    needs_human_confirmation: bool = True
    qa_ra_review_status: str = "Draft only - not approved"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


AIJsonCallable = Callable[..., dict[str, Any]]


def rank_document_sections(
    document: dict[str, Any],
    accepted_changes: list[dict[str, Any]],
    limit: int = MAX_SECTIONS_PER_DOCUMENT,
) -> list[RankedSection]:
    changes = ensure_evidence_ids(accepted_changes)
    change_terms = {change["evidence_id"]: content_terms(change_text(change)) for change in changes}
    ranked: list[RankedSection] = []
    excluded_ids = reference_section_ids(document)

    for section in document.get("sections", []):
        section_id = str(section.get("section_id", ""))
        location = str(section.get("page_or_location", ""))
        text = str(section.get("text", "")).strip()
        if not section_is_reviewable(section_id, location, text, excluded_ids):
            continue
        section_terms = content_terms(text)
        evidence_ids: list[str] = []
        matched_terms: set[str] = set()
        score = 0
        normalized_section = normalize_text(text)
        for evidence_id, terms in change_terms.items():
            shared = terms & section_terms
            if not shared:
                continue
            evidence_ids.append(evidence_id)
            matched_terms.update(shared)
            score += len(shared) * 2
            change = next(item for item in changes if item["evidence_id"] == evidence_id)
            for phrase in important_phrases(change):
                if phrase and phrase in normalized_section:
                    score += 4
        if score <= 0:
            continue
        ranked.append(
            RankedSection(
                section_id=section_id,
                location=location,
                text=text[:MAX_SECTION_CHARS],
                score=score,
                matched_terms=sorted(matched_terms),
                evidence_ids=sorted(set(evidence_ids)),
            )
        )

    return sorted(ranked, key=lambda item: (-item.score, item.section_id))[: max(limit, 0)]


def generate_document_suggestion(
    candidate: dict[str, Any],
    document: dict[str, Any],
    accepted_changes: list[dict[str, Any]],
    ai_call: AIJsonCallable = ai_json,
) -> dict[str, Any]:
    changes = ensure_evidence_ids(accepted_changes)
    sections = rank_document_sections(document, changes)
    base = suggestion_base(candidate, document)
    if not sections:
        return AmendmentSuggestion(
            **base,
            decision="No content-based amendment suggested",
            confidence="High",
            reason="No non-reference controlled-document section contained substantive content matching the accepted regulation changes.",
            suggestions=[],
            limitations=["Section 3 presence alone is not amendment evidence."],
        ).to_dict()

    selected_evidence_ids = {evidence_id for section in sections for evidence_id in section.evidence_ids}
    selected_changes = [change for change in changes if change["evidence_id"] in selected_evidence_ids][
        :MAX_CHANGES_PER_DOCUMENT
    ]
    prompt = suggestion_prompt(candidate, document, sections, selected_changes)
    fallback = {
        "decision": "Need confirmation",
        "confidence": "Low",
        "reason": "The AI response could not be validated as structured content-based amendment evidence.",
        "suggestions": [],
        "limitations": ["AI output requires QA/RA confirmation."],
    }
    raw = ai_call(prompt, fallback, timeout=180)
    validated = validate_ai_suggestion(raw, sections, selected_changes)
    return AmendmentSuggestion(
        **base,
        decision=validated["decision"],
        confidence=validated["confidence"],
        reason=validated["reason"],
        suggestions=validated["suggestions"],
        limitations=validated["limitations"],
    ).to_dict()


def validate_ai_suggestion(
    raw: dict[str, Any],
    sections: list[RankedSection],
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    section_by_id = {section.section_id: section for section in sections}
    valid_evidence_ids = {str(change.get("evidence_id", "")) for change in changes}
    valid_suggestions: list[dict[str, Any]] = []
    for item in raw.get("suggestions", []) if isinstance(raw.get("suggestions"), list) else []:
        if not isinstance(item, dict):
            continue
        section_id = str(item.get("section_id", ""))
        evidence_ids = [str(value) for value in item.get("evidence_ids", []) if str(value)]
        if section_id not in section_by_id or not evidence_ids or any(value not in valid_evidence_ids for value in evidence_ids):
            continue
        proposed_text = str(item.get("proposed_text", "")).strip()
        if not proposed_text:
            continue
        section = section_by_id[section_id]
        valid_suggestions.append(
            {
                "section_id": section_id,
                "location": section.location,
                "current_text": section.text,
                "proposed_text": proposed_text,
                "change_type": str(item.get("change_type", "revise")),
                "rationale": str(item.get("rationale", "")),
                "evidence_ids": evidence_ids,
                "matched_terms": section.matched_terms,
            }
        )

    requested_decision = str(raw.get("decision", "Need confirmation"))
    if valid_suggestions:
        decision = "Affected"
    elif requested_decision == "No content-based amendment suggested":
        decision = requested_decision
    else:
        decision = "Need confirmation"
    confidence = str(raw.get("confidence", "Low"))
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Low"
    limitations = [str(item) for item in raw.get("limitations", []) if str(item).strip()]
    if not valid_suggestions and decision != "No content-based amendment suggested":
        limitations.append("No AI amendment row passed section and regulation-evidence validation.")
    limitations.append("Human QA/RA confirmation is required before any controlled-document change.")
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(raw.get("reason", "")),
        "suggestions": valid_suggestions,
        "limitations": list(dict.fromkeys(limitations)),
    }


def suggestion_prompt(
    candidate: dict[str, Any],
    document: dict[str, Any],
    sections: list[RankedSection],
    changes: list[dict[str, Any]],
) -> str:
    payload = {
        "dependency": {
            "depth": candidate.get("depth", 0),
            "seed_code": candidate.get("seed_code", ""),
            "relationship_path": candidate.get("relationship_path", ""),
            "section3_evidence": candidate.get("section3_evidence", []),
        },
        "document": {
            "document_code": document.get("document_code", ""),
            "document_title": document.get("document_title", ""),
            "file_path": document.get("source_file_path") or document.get("file_path", ""),
        },
        "accepted_regulation_changes": changes,
        "candidate_sections": [section.to_dict() for section in sections],
    }
    return f"""
You are a cautious medical-device QA/RA controlled-document amendment assistant.

Use only the supplied accepted regulation changes and actual controlled-document section text.
Section 3 dependency presence is routing evidence only, never sufficient amendment evidence.
Do not use unmatched, editorial-only, renumber-only, unresolved, new-only, or deleted-only comparison noise.
Do not invent a clause, obligation, section, or proposed wording.
Preserve the target section's Chinese/English language pattern and terminology.
If no actual section content needs revision, return "No content-based amendment suggested" and no suggestions.
Every suggestion must cite one supplied section_id and one or more supplied evidence_ids.
All output is draft-only and requires QA/RA confirmation.

Evidence package:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Return valid JSON only:
{{
  "decision": "Affected | No content-based amendment suggested | Need confirmation",
  "confidence": "High | Medium | Low",
  "reason": "",
  "suggestions": [
    {{
      "section_id": "",
      "proposed_text": "",
      "change_type": "revise | add | delete | need_confirmation",
      "rationale": "",
      "evidence_ids": [""]
    }}
  ],
  "limitations": []
}}
""".strip()


def ensure_evidence_ids(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for position, item in enumerate(changes):
        change = dict(item)
        if not change.get("evidence_id"):
            identity = "|".join(
                [
                    str(change.get("old_clause_id", "")),
                    str(change.get("latest_clause_id", "")),
                    str(change.get("official_clause_id", "")),
                    str(position),
                ]
            )
            change["evidence_id"] = "change-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        result.append(change)
    return result


def suggestion_base(candidate: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    relationship_path = candidate.get("relationship_path", "")
    if isinstance(relationship_path, list):
        relationship_path = " -> ".join(str(value) for value in relationship_path)
    return {
        "document_code": str(document.get("document_code", candidate.get("document_code", ""))),
        "document_title": str(document.get("document_title", candidate.get("document_title", ""))),
        "file_path": str(
            document.get("source_file_path")
            or document.get("file_path")
            or candidate.get("file_path", "")
        ),
        "depth": int(candidate.get("depth", 0) or 0),
        "seed_code": str(candidate.get("seed_code", "")),
        "relationship_path": str(relationship_path),
    }


def reference_section_ids(document: dict[str, Any]) -> set[str]:
    sections = document.get("sections", [])
    ids = [str(section.get("section_id", "")) for section in sections]
    start = str(document.get("reference_section_start_id", ""))
    end = str(document.get("reference_section_end_id", ""))
    if start not in ids:
        return set()
    start_index = ids.index(start)
    end_index = ids.index(end) if end in ids else start_index
    if end_index < start_index:
        end_index = start_index
    return set(ids[start_index : end_index + 1])


def section_is_reviewable(section_id: str, location: str, text: str, excluded_ids: set[str]) -> bool:
    if not section_id or section_id in excluded_ids or len(text) < 30:
        return False
    if location.casefold().startswith(("header", "footer")):
        return False
    heading = normalize_text(text.splitlines()[0][:160])
    if any(value in heading for value in EXCLUDED_SECTION_HEADINGS):
        return False
    if re.match(r"^3(?:\.0+)?\s+(?:references?|參考文件|参考文件)\b", heading, re.I):
        return False
    return True


def content_terms(text: str) -> set[str]:
    terms = {
        token.casefold()
        for token in ENGLISH_TOKEN_RE.findall(str(text or ""))
        if token.casefold() not in GENERIC_TERMS and not token.isdigit()
    }
    for sequence in CHINESE_TOKEN_RE.findall(str(text or "")):
        if sequence not in GENERIC_TERMS:
            terms.add(sequence)
        for size in (2, 3, 4):
            if len(sequence) >= size:
                terms.update(sequence[index : index + size] for index in range(len(sequence) - size + 1))
    return terms


def change_text(change: dict[str, Any]) -> str:
    values: list[str] = []
    for key in [
        "old_clause_title",
        "latest_clause_title",
        "old_clause_text",
        "latest_clause_text",
        "official_change_comment",
        "change_comment",
        "evidence_preview",
    ]:
        values.append(str(change.get(key, "")))
    for key in ["added_requirement_units", "removed_requirement_units", "changed_requirement_units", "changed_terms"]:
        value = change.get(key, [])
        if isinstance(value, list):
            values.extend(str(item) for item in value)
    return "\n".join(value for value in values if value)


def important_phrases(change: dict[str, Any]) -> list[str]:
    phrases = []
    for key in ["old_clause_title", "latest_clause_title"]:
        value = normalize_text(str(change.get(key, "")))
        if len(value) >= 8:
            phrases.append(value)
    return phrases


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()
