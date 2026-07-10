from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .reference_graph import GENERIC_TOPIC_WORDS
from .regulation_normalizer import alias_regex, aliases_for


PAIRING_COLUMNS = [
    "source_file",
    "source_file_path",
    "document_code",
    "document_title",
    "filename_chinese_text",
    "reg_index",
    "original_standard_code",
    "latest_standard_code",
    "status",
    "change_summary",
    "match_method",
    "confidence",
    "evidence_text",
    "source_field",
    "review_required",
]

CHINESE_STOPWORDS = {
    "文件",
    "记录",
    "記錄",
    "控制",
    "管理",
    "程序",
    "操作",
    "指引",
    "质量",
    "品質",
    "工作",
    "作业",
    "作業",
    "标准",
    "標準",
    "体系",
    "系統",
    "系统",
}
CODE_PREFIX_RE = re.compile(r"^(?:QM\d{2}|QSP\d{4}|WI-\d{2}-[A-Z]{3}-\d{2}|FM\d+[A-Z]?)(?:-[A-Z])?\s*", re.I)
REVISION_TOKEN_RE = re.compile(r"\b[A-Z]\b|\b\d{6,8}\b")
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]+")
ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9/-]{3,}")


def build_file_regulation_pairing_map(
    updated_regulations: pd.DataFrame,
    documents: list[dict[str, Any]],
    amendment_profiles: list[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    if updated_regulations.empty or not documents:
        return pd.DataFrame(columns=PAIRING_COLUMNS)

    rows: list[dict[str, Any]] = []
    section3_keys: set[tuple[str, int]] = set()
    profiles = profiles_by_index(amendment_profiles)

    regulations = updated_only(updated_regulations).reset_index(drop=True)

    for reg_index, reg in regulations.iterrows():
        for document in documents:
            match = section3_reference_match(reg, document)
            if not match:
                continue
            section3_keys.add((document_key(document), reg_index))
            rows.append(pairing_row(reg_index, reg, document, "section3_reference", "High", *match, review_required=False))

    for reg_index, reg in regulations.iterrows():
        profile = profiles.get(reg_index, {})
        for document in documents:
            key = (document_key(document), reg_index)
            if key in section3_keys:
                continue
            match = chinese_filename_match(reg, document, profile)
            if not match:
                continue
            confidence, evidence_text, source_field = match
            rows.append(pairing_row(reg_index, reg, document, "chinese_filename", confidence, evidence_text, source_field, review_required=False))

    if not rows:
        return pd.DataFrame(columns=PAIRING_COLUMNS)
    return pd.DataFrame(rows, columns=PAIRING_COLUMNS).drop_duplicates(
        subset=["source_file_path", "reg_index", "match_method", "evidence_text"]
    ).reset_index(drop=True)


def section3_reference_match(reg: pd.Series, document: dict[str, Any]) -> tuple[str, str] | None:
    fields = ["reference_regulations", "reference_standards", "reference_guidance"]
    aliases = [item for item in aliases_for(reg.get("原标准编号", ""), reg.get("最新版本", "")) if item.get("strength") == "strong"]
    for field in fields:
        values = [str(item) for item in document.get(field, []) if str(item).strip()]
        text = "\n".join(values)
        if not text:
            continue
        for alias in aliases:
            match = alias_regex(alias["alias"]).search(text)
            if match:
                return match.group(0), field
    return None


def chinese_filename_match(reg: pd.Series, document: dict[str, Any], amendment_profile: dict[str, Any]) -> tuple[str, str, str] | None:
    filename_text = filename_match_text(document)
    filename_terms = meaningful_terms(filename_text)
    if not filename_terms:
        return None

    profile_values = list_values(amendment_profile.get("affected_qms_topics", [])) + list_values(amendment_profile.get("search_keywords", []))
    profile_terms = meaningful_terms(" ".join(profile_values))
    matched_profile_terms = sorted(filename_terms & profile_terms)
    if matched_profile_terms:
        return "Medium", ", ".join(matched_profile_terms[:5]), "amendment_profile"

    summary_terms = meaningful_terms(str(reg.get("主要变更内容", "")))
    matched_summary_terms = sorted(filename_terms & summary_terms)
    if matched_summary_terms:
        return "Low", ", ".join(matched_summary_terms[:5]), "change_summary"
    return None


def pairing_row(
    reg_index: int,
    reg: pd.Series,
    document: dict[str, Any],
    match_method: str,
    confidence: str,
    evidence_text: str,
    source_field: str,
    review_required: bool,
) -> dict[str, Any]:
    source_file = str(document.get("source_file_name") or document.get("file_name", ""))
    source_path = str(document.get("source_file_path") or document.get("file_path", ""))
    return {
        "source_file": source_file,
        "source_file_path": source_path,
        "document_code": str(document.get("document_code", "")),
        "document_title": str(document.get("document_title", "")),
        "filename_chinese_text": " ".join(chinese_sequences(filename_match_text(document))),
        "reg_index": reg_index,
        "original_standard_code": str(reg.get("原标准编号", "")),
        "latest_standard_code": str(reg.get("最新版本", "")),
        "status": str(reg.get("状态", "")),
        "change_summary": str(reg.get("主要变更内容", "")),
        "match_method": match_method,
        "confidence": confidence,
        "evidence_text": evidence_text,
        "source_field": source_field,
        "review_required": bool(review_required),
    }


def filename_match_text(document: dict[str, Any]) -> str:
    values = [
        str(document.get("source_file_name") or document.get("file_name", "")),
        str(document.get("document_title", "")),
    ]
    cleaned_values = []
    for value in values:
        stem = Path(value).stem
        stem = CODE_PREFIX_RE.sub("", stem)
        stem = REVISION_TOKEN_RE.sub(" ", stem)
        cleaned_values.append(stem)
    return " ".join(cleaned_values)


def meaningful_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for sequence in chinese_sequences(text):
        reduced = sequence
        for stopword in CHINESE_STOPWORDS:
            reduced = reduced.replace(stopword, " ")
        for item in CHINESE_RE.findall(reduced):
            if len(item) >= 2 and item not in CHINESE_STOPWORDS:
                terms.add(item)
        if len(sequence) >= 2 and sequence not in CHINESE_STOPWORDS:
            terms.add(sequence)

    for token in ENGLISH_TOKEN_RE.findall(str(text or "")):
        normalized = token.casefold()
        if len(normalized) < 4 or normalized in GENERIC_TOPIC_WORDS:
            continue
        terms.add(normalized)
    return terms


def chinese_sequences(text: str) -> list[str]:
    return CHINESE_RE.findall(str(text or ""))


def list_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            if isinstance(loaded, list):
                return [str(item) for item in loaded]
        except json.JSONDecodeError:
            pass
        return [value]
    return []


def profiles_by_index(amendment_profiles: list[dict[str, Any]] | None) -> dict[int, dict[str, Any]]:
    return {index: profile for index, profile in enumerate(amendment_profiles or [])}


def document_key(document: dict[str, Any]) -> str:
    return str(document.get("source_file_path") or document.get("file_path") or document.get("document_code", ""))


def updated_only(regulations: pd.DataFrame) -> pd.DataFrame:
    if "状态" not in regulations.columns:
        return regulations
    return regulations[regulations["状态"].astype(str).str.contains("已更新", na=False)].copy()
