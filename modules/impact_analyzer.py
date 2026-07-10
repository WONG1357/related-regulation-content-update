from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .ai_document_agent import review_regulation_document_pair, summarize_document
from .document_cache import DEFAULT_CACHE_PATH, get_or_create_profile
from .reference_graph import GENERIC_TOPIC_WORDS, attach_reference_matches
from .regulation_normalizer import alias_regex, aliases_for, normalize_standard_name


DocumentAnalyzer = Callable[[dict[str, Any]], dict[str, Any]]
PairReviewer = Callable[[dict[str, Any]], dict[str, Any]]

REVIEW_LEVELS = {
    "Level 1 - Direct regulation reference",
    "Level 2 - Direct standard/guidance reference",
    "Level 3 - Referenced QSP/WI relationship",
    "Level 4 - Topic/category match",
}


def analyze_impact(
    updated_regulations: pd.DataFrame,
    documents: list[dict[str, Any]],
    cache_path: str | Path = DEFAULT_CACHE_PATH,
    document_analyzer: DocumentAnalyzer | None = None,
    pair_reviewer: PairReviewer | None = None,
    amendment_profiles: list[dict[str, Any]] | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> pd.DataFrame:
    if updated_regulations.empty or not documents:
        return pd.DataFrame()

    candidates = select_candidate_pairs(updated_regulations, documents, amendment_profiles=amendment_profiles)
    review_candidates = candidates[candidates["review_required"] == True]  # noqa: E712 - pandas boolean mask
    if review_candidates.empty:
        return pd.DataFrame()

    documents_by_path = {str(document.get("file_path", "")): document for document in documents}
    profile_cache: dict[str, dict[str, Any]] = {}

    rows: list[dict[str, Any]] = []
    total_candidates = len(review_candidates)
    if progress_callback:
        progress_callback(0, total_candidates, "Preparing AI review")
    for completed, (_, candidate) in enumerate(review_candidates.iterrows(), start=1):
        document = documents_by_path.get(str(candidate.get("affected_file_path", "")))
        if not document:
            if progress_callback:
                progress_callback(completed, total_candidates, f"Skipped missing document {completed}/{total_candidates}")
            continue
        row = review_one_candidate(
            candidate,
            document,
            cache_path=cache_path,
            document_analyzer=document_analyzer,
            pair_reviewer=pair_reviewer,
            profile_cache=profile_cache,
        )
        if row:
            rows.append(row)
        if progress_callback:
            progress_callback(completed, total_candidates, f"Reviewed pair {completed}/{total_candidates}")

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(
        subset=["regulation_old", "affected_file_path", "ai_decision", "candidate_level"]
    ).reset_index(drop=True)


def review_one_candidate(
    candidate: pd.Series,
    document: dict[str, Any],
    cache_path: str | Path = DEFAULT_CACHE_PATH,
    document_analyzer: DocumentAnalyzer | None = None,
    pair_reviewer: PairReviewer | None = None,
    profile_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    analyze_document = document_analyzer or summarize_document
    review_pair = pair_reviewer or review_regulation_document_pair
    cache = profile_cache if profile_cache is not None else {}
    file_path = str(document.get("file_path", ""))
    if file_path not in cache:
        cache[file_path] = get_or_create_profile(document, analyze_document, cache_path)
    profile = cache[file_path]
    reg = candidate_to_reg(candidate)
    amendment_profile = profile_from_candidate(candidate)
    alias_evidence = collect_alias_evidence(reg, document)
    review_input = make_review_input(reg, profile, alias_evidence, candidate, amendment_profile)
    review = review_pair(review_input)
    decision = str(review.get("ai_decision", "")).strip()
    if decision not in {"Affected", "Need confirmation"}:
        return None
    return make_output_row(reg, profile, alias_evidence, review, candidate, amendment_profile)


def select_candidate_pairs(
    updated_regulations: pd.DataFrame,
    documents: list[dict[str, Any]],
    amendment_profiles: list[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    if updated_regulations.empty or not documents:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    direct_by_reg: dict[int, set[str]] = {}
    profiles_by_reg_index = profiles_by_index(amendment_profiles)
    graph = attach_reference_matches(documents)
    for reg_index, reg in updated_regulations.reset_index(drop=True).iterrows():
        amendment_profile = profiles_by_reg_index.get(reg_index, {})
        direct_codes: set[str] = set()
        for document in documents:
            direct = direct_reference_candidate(reg, document)
            if direct:
                direct_codes.add(str(document.get("document_code", "")).upper())
                rows.append(candidate_row(reg_index, reg, document, *direct, amendment_profile=amendment_profile))
        direct_by_reg[reg_index] = direct_codes

    existing_keys = {(row["reg_index"], row["affected_file_path"]) for row in rows}
    for reg_index, reg in updated_regulations.reset_index(drop=True).iterrows():
        amendment_profile = profiles_by_reg_index.get(reg_index, {})
        affected_codes = direct_by_reg.get(reg_index, set())
        for document in documents:
            key = (reg_index, str(document.get("file_path", "")))
            if key in existing_keys:
                continue
            indirect = indirect_reference_candidate(document, affected_codes, graph)
            if indirect:
                rows.append(candidate_row(reg_index, reg, document, *indirect, amendment_profile=amendment_profile))
                existing_keys.add(key)
                continue
            topic = topic_candidate(reg, document, amendment_profile)
            if topic:
                rows.append(candidate_row(reg_index, reg, document, *topic, amendment_profile=amendment_profile))
                existing_keys.add(key)
                continue
            weak = weak_candidate(reg, document, amendment_profile)
            if weak:
                rows.append(candidate_row(reg_index, reg, document, *weak, amendment_profile=amendment_profile))
                existing_keys.add(key)
                continue
            rows.append(
                candidate_row(
                    reg_index,
                    reg,
                    document,
                    "Skipped",
                    "No direct Section 3 reference, indirect document relationship, or strong topic evidence.",
                    "",
                    "",
                    False,
                    "No relationship evidence found.",
                    amendment_profile=amendment_profile,
                )
            )

    return pd.DataFrame(rows)


def direct_reference_candidate(reg: pd.Series, document: dict[str, Any]) -> tuple[str, str, str, str, bool, str] | None:
    reference_groups = [
        ("reference_regulations", "Level 1 - Direct regulation reference"),
        ("reference_standards", "Level 2 - Direct standard/guidance reference"),
        ("reference_guidance", "Level 2 - Direct standard/guidance reference"),
    ]
    for field, level in reference_groups:
        text = "\n".join(str(item) for item in document.get(field, []))
        evidence = first_alias_match(reg, text, strengths={"strong"})
        if evidence:
            reason = f"Section 3 {field} directly references {evidence['alias']}."
            return level, reason, str(evidence["matched_text"]), "", True, ""
    return None


def indirect_reference_candidate(
    document: dict[str, Any],
    affected_codes: set[str],
    graph: dict[str, Any] | None = None,
) -> tuple[str, str, str, str, bool, str] | None:
    if not affected_codes:
        return None
    references = {str(item).upper() for item in document.get("document_relationships", [])}
    matched = sorted(references & affected_codes)
    if not matched:
        return None
    graph = graph or {}
    path_parts = []
    for code in matched:
        linked = graph.get("file_index", {}).get(code, {}) if isinstance(graph.get("file_index", {}), dict) else {}
        path_parts.append(f"{document.get('document_code', '')} -> {code} ({linked.get('file_name', 'unresolved')})")
    path = "; ".join(path_parts)
    reason = f"Section 3 references affected controlled document(s): {', '.join(matched)}."
    return "Level 3 - Referenced QSP/WI relationship", reason, ", ".join(matched), path, True, ""


def topic_candidate(
    reg: pd.Series,
    document: dict[str, Any],
    amendment_profile: dict[str, Any] | None = None,
) -> tuple[str, str, str, str, bool, str] | None:
    haystack = "\n".join(
        [
            str(document.get("document_title", "")),
            non_reference_text(document, max_chars=8000),
        ]
    )
    profile_evidence = first_profile_keyword_match(amendment_profile or {}, haystack)
    if profile_evidence:
        reason = f"Amendment profile topic/keyword appears in document content: {profile_evidence}."
        return "Level 4 - Topic/category match", reason, profile_evidence, "", True, ""
    evidence = first_alias_match(reg, haystack, strengths={"strong"})
    if not evidence:
        return None
    reason = f"Strong regulation/topic alias appears outside Section 3: {evidence['alias']}."
    return "Level 4 - Topic/category match", reason, str(evidence["matched_text"]), "", True, ""


def weak_candidate(
    reg: pd.Series,
    document: dict[str, Any],
    amendment_profile: dict[str, Any] | None = None,
) -> tuple[str, str, str, str, bool, str] | None:
    profile_evidence = first_profile_keyword_match(amendment_profile or {}, document_text(document, max_chars=8000), weak=True)
    if profile_evidence:
        reason = f"Weak amendment-profile keyword match only: {profile_evidence}."
        return "Level 5 - Weak keyword match only", reason, profile_evidence, "", False, "Weak amendment-profile keyword evidence only; not sent to AI review."
    evidence = first_alias_match(reg, document_text(document, max_chars=8000), strengths={"weak"})
    if not evidence:
        return None
    reason = f"Weak keyword match only: {evidence['alias']}."
    return "Level 5 - Weak keyword match only", reason, str(evidence["matched_text"]), "", False, "Weak keyword evidence only; not sent to AI review."


def candidate_row(
    reg_index: int,
    reg: pd.Series,
    document: dict[str, Any],
    candidate_level: str,
    candidate_reason: str,
    reference_evidence: str,
    relationship_path: str,
    review_required: bool,
    skip_reason: str,
    amendment_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amendment_profile = amendment_profile or {}
    review_required, skip_reason = apply_amendment_profile_review_gate(review_required, skip_reason, amendment_profile)
    return {
        "reg_index": reg_index,
        "regulation_old": str(reg.get("原标准编号", "")),
        "regulation_old_normalized": normalize_standard_name(reg.get("原标准编号", "")),
        "regulation_latest": str(reg.get("最新版本", "")),
        "regulation_latest_normalized": normalize_standard_name(reg.get("最新版本", "")),
        "status": str(reg.get("状态", "")),
        "latest_year": str(reg.get("最新年份", "")),
        "change_summary": str(reg.get("主要变更内容", "")),
        "reference_source": str(reg.get("查询参考", "")),
        "affected_file": str(document.get("file_name", "")),
        "affected_file_path": str(document.get("file_path", "")),
        "document_code": str(document.get("document_code", "")),
        "document_title": str(document.get("document_title", "")),
        "document_type": str(document.get("document_type", "")),
        "candidate_level": candidate_level,
        "candidate_reason": candidate_reason,
        "relationship_path": relationship_path,
        "reference_evidence": reference_evidence,
        "review_required": review_required,
        "skip_reason": skip_reason,
        "amendment_profile": json.dumps(amendment_profile, ensure_ascii=False),
        "amendment_source_mode": str(amendment_profile.get("source_mode", "")),
        "amendment_confidence": str(amendment_profile.get("confidence", "")),
        "exact_clause_verified": bool(amendment_profile.get("exact_clause_verified", False)),
        "needs_human_confirmation": bool(amendment_profile.get("needs_human_confirmation", False)),
        "amendment_summary_used": str(amendment_profile.get("amendment_summary_used", "")),
        "amendment_topics": json.dumps(amendment_profile.get("affected_qms_topics", []), ensure_ascii=False),
        "amendment_search_keywords": json.dumps(amendment_profile.get("search_keywords", []), ensure_ascii=False),
    }


def apply_amendment_profile_review_gate(
    review_required: bool,
    skip_reason: str,
    amendment_profile: dict[str, Any],
) -> tuple[bool, str]:
    if not review_required:
        return review_required, skip_reason
    if amendment_profile.get("source_mode") != "old_vs_latest_file_comparison":
        return review_required, skip_reason
    review_classes = [str(item) for item in amendment_profile.get("ai_review_change_classes", [])]
    if review_classes:
        return True, skip_reason
    return (
        False,
        "Old-vs-latest regulation comparison found no substantive or selected editorial clause changes for AI review.",
    )


def collect_alias_evidence(reg: pd.Series, document: dict[str, Any]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for alias in aliases_for(reg.get("原标准编号", ""), reg.get("最新版本", "")):
        pattern = alias_regex(alias["alias"])
        for section in document.get("sections", []):
            text = str(section.get("text", ""))
            match = pattern.search(text)
            if not match:
                continue
            evidence.append(
                {
                    "alias": alias["alias"],
                    "strength": alias["strength"],
                    "matched_text": match.group(0),
                    "section_id": str(section.get("section_id", "")),
                    "location": str(section.get("page_or_location", "")),
                    "excerpt": text[:500],
                }
            )
            break
    return evidence


def make_review_input(
    reg: pd.Series,
    profile: dict[str, Any],
    alias_evidence: list[dict[str, str]],
    candidate: pd.Series,
    amendment_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amendment_profile = amendment_profile or {}
    return {
        "old_standard": str(reg.get("原标准编号", "")),
        "latest_standard": str(reg.get("最新版本", "")),
        "latest_year": str(reg.get("最新年份", "")),
        "status": str(reg.get("状态", "")),
        "change_summary": str(reg.get("主要变更内容", "")),
        "reference_source": str(reg.get("查询参考", "")),
        "document_code": profile.get("document_code", ""),
        "document_title": profile.get("document_title", ""),
        "document_type": profile.get("document_type", ""),
        "file_name": profile.get("file_name", ""),
        "content_summary": profile.get("content_summary", ""),
        "key_sections": profile.get("key_sections", []),
        "main_sections": profile.get("main_sections", []),
        "section_3_references": {
            "reference_regulations": profile.get("reference_regulations", []),
            "reference_standards": profile.get("reference_standards", []),
            "reference_guidance": profile.get("reference_guidance", []),
            "reference_documents": profile.get("reference_documents", []),
            "reference_forms": profile.get("reference_forms", []),
        },
        "candidate_level": candidate.get("candidate_level", ""),
        "candidate_reason": candidate.get("candidate_reason", ""),
        "relationship_path": candidate.get("relationship_path", ""),
        "reference_evidence": candidate.get("reference_evidence", ""),
        "relevant_non_reference_sections": profile.get("key_sections", []),
        "alias_evidence": alias_evidence,
        "amendment_profile": amendment_profile,
        "amendment_source_mode": amendment_profile.get("source_mode", ""),
        "amendment_confidence": amendment_profile.get("confidence", ""),
        "amendment_summary_used": amendment_profile.get("amendment_summary_used", ""),
        "exact_clause_verified": amendment_profile.get("exact_clause_verified", False),
        "needs_human_confirmation": amendment_profile.get("needs_human_confirmation", True),
        "amendment_limitations": amendment_profile.get("limitations", []),
        "affected_qms_topics": amendment_profile.get("affected_qms_topics", []),
        "search_keywords": amendment_profile.get("search_keywords", []),
    }


def make_output_row(
    reg: pd.Series,
    profile: dict[str, Any],
    alias_evidence: list[dict[str, str]],
    review: dict[str, Any],
    candidate: pd.Series,
    amendment_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    amendment_profile = amendment_profile or {}
    strongest = strongest_alias(alias_evidence)
    locations = review.get("recommended_amendment_locations", [])
    return {
        "regulation_old": str(reg.get("原标准编号", "")),
        "regulation_old_normalized": normalize_standard_name(reg.get("原标准编号", "")),
        "regulation_latest": str(reg.get("最新版本", "")),
        "regulation_latest_normalized": normalize_standard_name(reg.get("最新版本", "")),
        "status": str(reg.get("状态", "")),
        "latest_year": str(reg.get("最新年份", "")),
        "change_summary": str(reg.get("主要变更内容", "")),
        "reference_source": str(reg.get("查询参考", "")),
        "affected_file": profile.get("file_name", ""),
        "affected_file_path": profile.get("file_path", ""),
        "document_code": profile.get("document_code", ""),
        "document_title": profile.get("document_title", ""),
        "document_type": profile.get("document_type", ""),
        "document_summary": profile.get("content_summary", ""),
        "key_sections": json.dumps(profile.get("key_sections", []), ensure_ascii=False),
        "main_sections": json.dumps(profile.get("main_sections", []), ensure_ascii=False),
        "reference_regulations": json.dumps(profile.get("reference_regulations", []), ensure_ascii=False),
        "reference_standards": json.dumps(profile.get("reference_standards", []), ensure_ascii=False),
        "reference_guidance": json.dumps(profile.get("reference_guidance", []), ensure_ascii=False),
        "reference_documents": json.dumps(profile.get("reference_documents", []), ensure_ascii=False),
        "reference_forms": json.dumps(profile.get("reference_forms", []), ensure_ascii=False),
        "candidate_level": candidate.get("candidate_level", ""),
        "candidate_reason": candidate.get("candidate_reason", ""),
        "relationship_path": candidate.get("relationship_path", ""),
        "reference_evidence": candidate.get("reference_evidence", ""),
        "review_required": bool(candidate.get("review_required", True)),
        "skip_reason": str(candidate.get("skip_reason", "")),
        "amendment_source_mode": amendment_profile.get("source_mode", candidate.get("amendment_source_mode", "")),
        "amendment_confidence": amendment_profile.get("confidence", candidate.get("amendment_confidence", "")),
        "exact_clause_verified": bool(amendment_profile.get("exact_clause_verified", candidate.get("exact_clause_verified", False))),
        "needs_human_confirmation": bool(amendment_profile.get("needs_human_confirmation", candidate.get("needs_human_confirmation", False))),
        "amendment_summary_used": amendment_profile.get("amendment_summary_used", candidate.get("amendment_summary_used", "")),
        "amendment_limitations": json.dumps(amendment_profile.get("limitations", []), ensure_ascii=False),
        "affected_qms_topics": json.dumps(amendment_profile.get("affected_qms_topics", []), ensure_ascii=False),
        "search_keywords": json.dumps(amendment_profile.get("search_keywords", []), ensure_ascii=False),
        "ai_decision": review.get("ai_decision", ""),
        "risk_level": review.get("risk_level", ""),
        "priority": review.get("ai_decision", ""),
        "reason": review.get("reason", ""),
        "recommended_amendment_locations": json.dumps(locations, ensure_ascii=False),
        "supporting_evidence": review.get("supporting_evidence", ""),
        "alias_evidence": json.dumps(alias_evidence, ensure_ascii=False),
        "matched_alias": strongest.get("alias", ""),
        "matched_text": strongest.get("matched_text", ""),
        "match_strength": strongest.get("strength", ""),
        "section_id": first_location_value(locations, "section_id"),
        "section_location": first_location_value(locations, "location"),
        "section_text": "",
        "ai_notes": review.get("notes", ""),
    }


def candidate_to_reg(candidate: pd.Series) -> pd.Series:
    return pd.Series(
        {
            "原标准编号": candidate.get("regulation_old", ""),
            "最新版本": candidate.get("regulation_latest", ""),
            "最新年份": candidate.get("latest_year", ""),
            "状态": candidate.get("status", ""),
            "主要变更内容": candidate.get("change_summary", ""),
            "查询参考": candidate.get("reference_source", ""),
        }
    )


def profiles_by_index(amendment_profiles: list[dict[str, Any]] | None) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for index, profile in enumerate(amendment_profiles or []):
        result[index] = profile
    return result


def profile_from_candidate(candidate: pd.Series) -> dict[str, Any]:
    raw = candidate.get("amendment_profile", "")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw)) if raw else {}
    except json.JSONDecodeError:
        return {}


def first_profile_keyword_match(amendment_profile: dict[str, Any], text: str, weak: bool = False) -> str:
    if not text:
        return ""
    haystack = text.casefold()
    topics = [str(item) for item in amendment_profile.get("affected_qms_topics", [])]
    keywords = [str(item) for item in amendment_profile.get("search_keywords", [])]
    values = keywords if weak else topics + keywords
    matches = []
    for value in values:
        value = value.strip()
        if len(value) < 3:
            continue
        if not weak and is_generic_topic_word(value):
            continue
        if value.casefold() in haystack:
            matches.append(value)
    if weak:
        return matches[0] if matches else ""
    if len(matches) >= 2:
        return ", ".join(matches[:3])
    if matches and strong_keyword_in_prominent_text(matches[0], text):
        return matches[0]
    return ""


def is_generic_topic_word(value: str) -> bool:
    normalized = re.sub(r"[^a-z]+", " ", value.casefold()).strip()
    words = [word for word in normalized.split() if word]
    return bool(words) and all(word in GENERIC_TOPIC_WORDS for word in words)


def strong_keyword_in_prominent_text(value: str, text: str) -> bool:
    prominent_lines = []
    for line in str(text).splitlines()[:80]:
        stripped = line.strip()
        if re.search(r"\b(?:scope|purpose|title|heading|范围|目的|标题)\b", stripped, re.I):
            prominent_lines.append(stripped)
    prominent = "\n".join(prominent_lines + str(text).splitlines()[:5])
    return value.casefold() in prominent.casefold()


def first_alias_match(reg: pd.Series, text: str, strengths: set[str]) -> dict[str, str] | None:
    if not text:
        return None
    for alias in aliases_for(reg.get("原标准编号", ""), reg.get("最新版本", "")):
        if alias["strength"] not in strengths:
            continue
        match = alias_regex(alias["alias"]).search(text)
        if match:
            return {"alias": alias["alias"], "strength": alias["strength"], "matched_text": match.group(0)}
    return None


def non_reference_text(document: dict[str, Any], max_chars: int | None = None) -> str:
    references = set()
    for field in ["reference_regulations", "reference_standards", "reference_guidance", "reference_documents", "reference_forms"]:
        references.update(str(item) for item in document.get(field, []))
    text = document_text(document, max_chars=max_chars)
    for value in references:
        text = text.replace(value, "")
    return text


def document_text(document: dict[str, Any], max_chars: int | None = None) -> str:
    parts = []
    total = 0
    for section in document.get("sections", []):
        text = str(section.get("text", ""))
        total += len(text)
        parts.append(text)
        if max_chars is not None and total >= max_chars:
            break
    result = "\n".join(parts)
    return result[:max_chars] if max_chars is not None else result


def strongest_alias(alias_evidence: list[dict[str, str]]) -> dict[str, str]:
    for item in alias_evidence:
        if item.get("strength") == "strong":
            return item
    return alias_evidence[0] if alias_evidence else {}


def first_location_value(locations: Any, key: str) -> str:
    if isinstance(locations, list) and locations:
        first = locations[0]
        if isinstance(first, dict):
            return str(first.get(key, ""))
    return ""


def classify_priority(reg: pd.Series, text: str, strength: str) -> tuple[str, str]:
    if strength == "weak":
        return "Low", "Weak Chinese keyword match only; use as low-confidence evidence."
    return "Low", "Alias evidence only; final impact priority is decided by AI pair review."
