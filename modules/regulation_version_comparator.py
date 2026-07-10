from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pandas as pd


BUNDLED_PYTHON_PACKAGES = Path(
    os.getenv(
        "CODEX_BUNDLED_PYTHON_PACKAGES",
        str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python"),
    )
).expanduser()
BUNDLED_SITE_PACKAGES = BUNDLED_PYTHON_PACKAGES / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


SUBSTANTIVE_CLASSES = {
    "modified_requirement",
    "expanded_requirement",
    "reduced_or_deleted_requirement",
    "renumbered_and_modified",
    "newly_added_clause",
    "deleted_clause",
    "split_or_merged_clause",
    "uncertain_need_confirmation",
}
EDITORIAL_CLASS = "editorial_change_only"
DIRECT_COMPARISON_METHOD = "direct_normative_clause_comparison"
OFFICIAL_COMPARISON_METHOD = "exact_edition_official_comparison"
ACCEPTED_SUBSTANTIVE_CLASSES = {
    "modified_requirement",
    "expanded_requirement",
    "reduced_or_deleted_requirement",
    "renumbered_and_modified",
    "split_or_merged_clause",
}

ISO_13485_KNOWN_RENUMBERINGS = {
    "1.1": "1",
    "4.2.3": "4.2.4",
    "4.2.4": "4.2.5",
    "6.2.2": "6.2",
    "7.5.1.2.1": "7.5.2",
    "7.5.1.2.2": "7.5.3",
    "7.5.1.2.3": "7.5.4",
    "7.5.1.3": "7.5.5",
    "7.5.2": "7.5.6",
    "7.5.2.2": "7.5.7",
    "7.5.3.2.1": "7.5.9.1",
    "7.5.3.2.2": "7.5.9.2",
    "7.3.7": "7.3.9",
    "8.2.4": "8.2.6",
    "8.3": "8.3.1",
}

ISO_13485_NEW_OR_SEPARATED_LATEST_CLAUSES = {
    "4.2.3",
    "7.3.8",
    "7.3.10",
    "8.2.2",
    "8.2.3",
    "8.3.4",
}

ISO_13485_2016_KNOWN_BODY_TITLES = {
    "4.2.3": "Medical device file",
    "4.2.4": "Control of documents",
    "4.2.5": "Control of records",
    "8.2.2": "Complaint handling",
    "8.2.3": "Reporting to regulatory authorities",
    "8.2.4": "Internal audit",
}

CLASSIFICATION_STRENGTH = {
    "uncertain_need_confirmation": 100,
    "split_or_merged_clause": 95,
    "newly_added_clause": 90,
    "deleted_clause": 90,
    "renumbered_and_modified": 85,
    "expanded_requirement": 80,
    "reduced_or_deleted_requirement": 75,
    "modified_requirement": 70,
    "renumbered_only": 30,
    EDITORIAL_CLASS: 20,
    "unchanged": 10,
}

REQUIREMENT_SIGNALS = [
    "shall",
    "shall establish",
    "shall document",
    "shall maintain",
    "shall retain",
    "shall validate",
    "shall determine",
    "shall monitor",
    "shall evaluate",
    "records shall be maintained",
    "documented procedure",
    "documented requirements",
    "required",
    "requirement",
    "must",
    "应",
    "应当",
    "必须",
    "要求",
    "记录",
    "程序",
    "文件化",
]

QMS_TOPIC_RULES: list[tuple[str, list[str], list[str]]] = [
    ("regulatory requirements / organization roles", ["regulatory requirement", "regulatory authority", "organization role"], ["regulatory requirements", "organization roles"]),
    ("risk-based QMS approach", ["risk", "risk-based", "risk management"], ["risk", "risk-based QMS"]),
    ("medical device file", ["medical device file", "device master", "technical file"], ["medical device file", "technical file"]),
    ("document and record control", ["document", "record", "documented", "retain"], ["documentation", "records"]),
    ("management review", ["management review"], ["management review"]),
    ("training / competence", ["training", "competence", "competency"], ["training", "competence"]),
    ("work environment and contamination control", ["work environment", "contamination", "cleanliness"], ["work environment", "contamination"]),
    ("design and development", ["design and development", "design input", "design output"], ["design and development"]),
    ("design transfer", ["design transfer"], ["design transfer"]),
    ("design changes", ["design change", "changes to design"], ["design changes"]),
    ("design files / DHF", ["design file", "design history file", "dhf"], ["design file", "DHF"]),
    ("supplier and purchasing control", ["supplier", "purchasing", "outsourced"], ["supplier", "purchasing"]),
    ("production and service provision", ["production", "service provision"], ["production", "service provision"]),
    ("sterilization / sterile barrier validation", ["sterilization", "sterile barrier"], ["sterilization", "sterile barrier"]),
    ("software validation", ["software", "validation of software"], ["software validation"]),
    ("traceability", ["traceability", "unique device identification", "udi"], ["traceability", "UDI"]),
    ("monitoring and measuring equipment", ["monitoring and measuring equipment", "calibration"], ["calibration", "measuring equipment"]),
    ("feedback", ["feedback"], ["feedback"]),
    ("complaint handling", ["complaint"], ["complaint handling"]),
    ("regulatory authority reporting", ["reporting to regulatory", "advisory notice"], ["regulatory reporting"]),
    ("nonconforming product", ["nonconforming product", "nonconformity"], ["nonconforming product"]),
    ("rework", ["rework"], ["rework"]),
    ("CAPA / preventive action", ["corrective action", "preventive action", "capa"], ["CAPA", "corrective action"]),
]


@dataclass
class ClauseBlock:
    clause_id: str
    clause_title: str
    clause_text: str
    page_start: str = ""
    page_end: str = ""
    source_version: str = ""
    source_file: str = ""


@dataclass(frozen=True)
class AcceptedClauseChangeEvidence:
    evidence_id: str
    regulation_id: str
    old_version: str
    latest_version: str
    old_clause_id: str
    old_clause_title: str
    old_clause_text: str
    latest_clause_id: str
    latest_clause_title: str
    latest_clause_text: str
    mapping_method: str
    change_classification: str
    evidence_method: str
    evidence_source: str
    official_change_summary: str
    added_requirement_units: list[str]
    removed_requirement_units: list[str]
    changed_requirement_units: list[str]
    affected_qms_topics: list[str]
    search_keywords: list[str]
    impact_level: str
    needs_human_confirmation: bool
    limitations: list[str]
    source_row: dict[str, Any]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AcceptedClauseChangeEvidence":
        return cls(
            evidence_id=evidence_id_for_row(row),
            regulation_id=str(row.get("regulation_id", "")),
            old_version=str(row.get("old_version", "")),
            latest_version=str(row.get("latest_version", "")),
            old_clause_id=str(row.get("old_clause_id", "")),
            old_clause_title=str(row.get("old_clause_title", "")),
            old_clause_text=str(row.get("old_clause_text", "")),
            latest_clause_id=str(row.get("latest_clause_id", "")),
            latest_clause_title=str(row.get("latest_clause_title", "")),
            latest_clause_text=str(row.get("latest_clause_text", "")),
            mapping_method=str(row.get("mapping_method", "")),
            change_classification=str(row.get("change_classification", "")),
            evidence_method=str(row.get("evidence_method", "")),
            evidence_source=str(row.get("evidence_source", "")),
            official_change_summary=str(row.get("official_change_summary", "")),
            added_requirement_units=list(row.get("added_requirement_units") or []),
            removed_requirement_units=list(row.get("removed_requirement_units") or []),
            changed_requirement_units=list(row.get("changed_requirement_units") or []),
            affected_qms_topics=list(row.get("affected_qms_topics") or []),
            search_keywords=list(row.get("search_keywords") or []),
            impact_level=str(row.get("impact_level", "")),
            needs_human_confirmation=bool(row.get("needs_human_confirmation")),
            limitations=list(row.get("limitations") or []),
            source_row=dict(row),
        )

    def to_dict(self) -> dict[str, Any]:
        output = dict(self.source_row)
        for field_name, value in asdict(self).items():
            if field_name != "source_row":
                output[field_name] = value
        return output


def compare_regulation_versions(
    old_file: Path,
    latest_file: Path,
    regulation_id: str,
    old_version: str,
    latest_version: str,
    include_editorial_changes: bool = False,
) -> dict[str, Any]:
    old_content = extract_regulation_content(old_file, capture_pdf_tables=False)
    latest_content = extract_regulation_content(latest_file, capture_pdf_tables=True)
    old_text = clean_regulation_text(old_content["text"])
    latest_text = clean_regulation_text(latest_content["text"])
    is_iso_13485 = is_iso_13485_regulation(regulation_id, old_file, latest_file)
    if is_iso_13485:
        old_clauses = extract_iso_13485_main_body_clauses(old_text, old_version, str(old_file))
        latest_clauses = extract_iso_13485_main_body_clauses(latest_text, latest_version, str(latest_file))
        annex_a_reference = extract_iso_13485_annex_a_mapping_reference(latest_text)
    else:
        old_clauses = extract_normative_clause_blocks(old_text, old_version, str(old_file))
        latest_clauses = extract_normative_clause_blocks(latest_text, latest_version, str(latest_file))
        annex_a_reference = {"detected": False, "mapping_rows": []}
    official = detect_official_comparison_material(
        latest_text,
        regulation_id=regulation_id,
        old_version=old_version,
        latest_version=latest_version,
        table_records=latest_content["tables"],
    )
    validate_official_rows_against_normative_clauses(official, old_clauses, latest_clauses)
    selected_method = OFFICIAL_COMPARISON_METHOD if official.get("usable_as_primary") else DIRECT_COMPARISON_METHOD
    official["selected_as_primary"] = selected_method == OFFICIAL_COMPARISON_METHOD
    if is_iso_13485:
        mappings = build_iso_13485_clause_mappings(old_clauses, latest_clauses, official)
    else:
        mappings = build_clause_mappings(old_clauses, latest_clauses, official)
    attach_official_evidence(mappings, official)
    raw_results = [
        classify_mapping(mapping, old_clauses, latest_clauses, regulation_id, old_version, latest_version)
        for mapping in mappings
    ]
    results = aggregate_clause_comparisons(raw_results)
    extraction_warnings = comparison_quality_warnings(results, old_clauses, latest_clauses, is_iso_13485, raw_results)
    validation_failures = comparison_validation_failures(results, old_clauses, latest_clauses, is_iso_13485)
    if validation_failures:
        extraction_warnings.extend(validation_failures)
    apply_extraction_warnings(results, extraction_warnings)
    export_results = [] if validation_failures else results
    accepted_changes: list[dict[str, Any]] = []
    for row in export_results:
        if not row.get("accepted_for_drafting"):
            continue
        evidence = AcceptedClauseChangeEvidence.from_row(row)
        row["evidence_id"] = evidence.evidence_id
        accepted_changes.append(evidence.to_dict())
    counts = classification_counts(export_results)
    reviewable = accepted_changes
    topics, keywords = extract_topics_and_keywords(reviewable)
    evidence_source = (
        official.get("source", "")
        if selected_method == OFFICIAL_COMPARISON_METHOD
        else "Uploaded old/latest normative clause text"
    )
    return {
        "regulation_id": regulation_id,
        "old_version": old_version,
        "latest_version": latest_version,
        "old_file": str(old_file),
        "latest_file": str(latest_file),
        "source_mode": "old_vs_latest_file_comparison",
        "selected_method": selected_method,
        "comparison_method": selected_method,
        "evidence_source": evidence_source,
        "official_comparison": official,
        "annex_a_mapping_reference": annex_a_reference,
        "old_body_clause_index": [asdict(item) for item in old_clauses],
        "latest_body_clause_index": [asdict(item) for item in latest_clauses],
        "old_clause_index": [asdict(item) for item in old_clauses],
        "latest_clause_index": [asdict(item) for item in latest_clauses],
        "raw_clause_comparisons": raw_results,
        "clause_comparisons": export_results,
        "accepted_changes": accepted_changes,
        "classification_counts": counts,
        "mapped_clause_count": len(export_results),
        "reviewable_change_count": len(reviewable),
        "reviewable_change_classes": sorted({row["change_classification"] for row in reviewable}),
        "include_editorial_changes": include_editorial_changes,
        "affected_qms_topics": topics,
        "search_keywords": keywords,
        "exact_clause_verified": bool(accepted_changes),
        "extraction_warnings": extraction_warnings,
        "validation_failures": validation_failures,
        "comparison_validation_failed": bool(validation_failures),
        "limitations": comparison_limitations(old_text, latest_text, old_clauses, latest_clauses, official, extraction_warnings),
    }


def extract_regulation_text(path: Path | None) -> str:
    return str(extract_regulation_content(path, capture_pdf_tables=False)["text"])


def extract_regulation_content(path: Path | None, capture_pdf_tables: bool = False) -> dict[str, Any]:
    if not path:
        return {"text": "", "tables": []}
    suffix = path.suffix.casefold()
    try:
        if suffix == ".txt":
            return {"text": path.read_text(encoding="utf-8", errors="ignore"), "tables": []}
        if suffix == ".docx":
            from docx import Document

            doc = Document(path)
            return {"text": "\n".join(paragraph.text for paragraph in doc.paragraphs), "tables": []}
        if suffix == ".xlsx":
            sheets = pd.read_excel(path, sheet_name=None, header=None)
            text = "\n".join(frame.fillna("").astype(str).agg(" ".join, axis=1).str.cat(sep="\n") for frame in sheets.values())
            return {"text": text, "tables": []}
        if suffix == ".pdf":
            return extract_pdf_content(path, capture_tables=capture_pdf_tables)
    except Exception:
        return {"text": "", "tables": []}
    return {"text": "", "tables": []}


def extract_pdf_content(path: Path, capture_tables: bool = False) -> dict[str, Any]:
    ensure_bundled_pdf_packages()
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            page_texts: list[str] = []
            for page in pdf.pages:
                try:
                    page_texts.append(page.extract_text() or "")
                except Exception:
                    page_texts.append("")
            table_records: list[dict[str, Any]] = []
            if capture_tables:
                for page_index, section_context in comparison_table_page_contexts(page_texts).items():
                    try:
                        tables = pdf.pages[page_index].extract_tables() or []
                    except Exception:
                        continue
                    for table in tables:
                        if table:
                            table_records.append(
                                {
                                    "page_number": page_index + 1,
                                    "page_text": page_texts[page_index],
                                    "section_context": section_context,
                                    "rows": table,
                                }
                            )
            return {"text": "\n".join(page_texts), "tables": table_records}
    except Exception:
        pass

    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader  # type: ignore
    try:
        reader = PdfReader(str(path))
        return {"text": "\n".join(page.extract_text() or "" for page in reader.pages), "tables": []}
    except Exception:
        return {"text": "", "tables": []}


def comparison_table_page_contexts(page_texts: list[str]) -> dict[int, str]:
    contexts: dict[int, str] = {}
    active_context = ""
    for page_index, page_text in enumerate(page_texts):
        lines = [normalize_whitespace(line) for line in page_text.splitlines() if normalize_whitespace(line)]
        page_prefix = "\n".join(lines[:24])
        annex_heading = next((line for line in lines[:8] if re.match(r"^annex\s+[a-z]\b", line, re.I)), "")
        if annex_heading:
            active_context = page_prefix if has_official_comparison_heading(page_prefix) else ""
        elif has_official_comparison_heading(page_prefix):
            active_context = page_prefix
        if active_context:
            contexts[page_index] = active_context
        if any(re.match(r"^bibliography\b", line, re.I) for line in lines[:5]):
            active_context = ""
    return contexts


def ensure_bundled_pdf_packages() -> None:
    for path in [BUNDLED_SITE_PACKAGES, BUNDLED_PYTHON_PACKAGES]:
        if path.exists() and str(path) not in sys.path:
            sys.path.append(str(path))


def clean_regulation_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).replace("\u00ad", "")
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", normalized)
    normalized = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", normalized)
    raw_lines = [normalize_whitespace(line) for line in normalized.splitlines()]
    line_counts: dict[str, int] = {}
    for line in raw_lines:
        if len(line) >= 8:
            line_counts[line.casefold()] = line_counts.get(line.casefold(), 0) + 1

    cleaned: list[str] = []
    for line in raw_lines:
        if not line:
            continue
        lowered = line.casefold()
        if line_counts.get(lowered, 0) >= 4 and len(line) < 120:
            continue
        if re.fullmatch(r"(page\s*)?\d+\s*(of\s*\d+)?", lowered):
            continue
        if "copyright" in lowered or "all rights reserved" in lowered:
            continue
        if re.match(
            r"^(downloaded (?:from|by|on)|licensed (?:to|for)|this (?:file|document) (?:was )?downloaded|provided by ihs|not for resale)\b",
            lowered,
        ):
            continue
        if re.search(r"\.{4,}\s*\d+$", line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def detect_official_comparison_material(
    text: str,
    regulation_id: str = "",
    old_version: str = "",
    latest_version: str = "",
    table_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    matches = official_comparison_sections(text)
    for match in matches:
        match["exact_editions"] = heading_names_uploaded_editions(
            match.get("excerpt", ""), regulation_id, old_version, latest_version
        )

    text_rows = extract_official_mapping_rows(matches)
    table_rows = extract_official_mapping_rows_from_tables(
        table_records or [], regulation_id, old_version, latest_version
    )
    rows = dedupe_official_rows(table_rows + text_rows)
    exact_rows = [row for row in rows if row.get("exact_editions")]
    exact_matches = [match for match in matches if match.get("exact_editions")]
    detected = bool(matches or table_records)
    usable_as_primary = bool(exact_matches and exact_rows)
    if usable_as_primary:
        rejection_reason = ""
    elif detected and not exact_matches:
        rejection_reason = "Official comparison material does not name both uploaded editions."
    elif detected:
        rejection_reason = "Exact-edition official comparison material was found, but no valid comparison rows were parsed."
    else:
        rejection_reason = "No official comparison material was detected."

    selected_match = exact_matches[0] if exact_matches else (matches[0] if matches else {})
    source = official_source_label(selected_match)
    return {
        "detected": detected,
        "exact_editions": bool(exact_matches),
        "usable_as_primary": usable_as_primary,
        "selected_as_primary": False,
        "source": source,
        "evidence_source": source,
        "matches": matches[:8],
        "mapping_rows": exact_rows if usable_as_primary else rows,
        "parsed_row_count": len(exact_rows if usable_as_primary else rows),
        "rejection_reason": rejection_reason,
    }


def has_official_comparison_heading(text: str) -> bool:
    return bool(
        re.search(
            r"comparison\s+of\s+content|correspondence\s+between|summary\s+of\s+(?:the\s+)?changes|revision\s+history|transition\s+note|comment\s+on\s+change",
            str(text or ""),
            re.I,
        )
    )


def validate_official_rows_against_normative_clauses(
    official: dict[str, Any],
    old_clauses: list[ClauseBlock],
    latest_clauses: list[ClauseBlock],
) -> None:
    if not official.get("usable_as_primary"):
        return
    old_ids = {clause.clause_id for clause in old_clauses}
    latest_ids = {clause.clause_id for clause in latest_clauses}
    rows = list(official.get("mapping_rows", []))
    valid_rows = [
        row
        for row in rows
        if str(row.get("latest_clause_id", "")) in latest_ids
        and (not row.get("old_clause_id") or str(row.get("old_clause_id")) in old_ids)
    ]
    official["syntactic_row_count"] = len(rows)
    official["invalid_row_count"] = len(rows) - len(valid_rows)
    official["mapping_rows"] = valid_rows
    official["parsed_row_count"] = len(valid_rows)
    if not valid_rows:
        official["usable_as_primary"] = False
        official["rejection_reason"] = "Official comparison rows did not resolve to the uploaded normative clauses."


def official_comparison_sections(text: str) -> list[dict[str, Any]]:
    lines = str(text or "").splitlines()
    matches: list[dict[str, Any]] = []
    annex_starts = [index for index, line in enumerate(lines) if is_annex_heading(line)]
    for offset, start in enumerate(annex_starts):
        stop = annex_starts[offset + 1] if offset + 1 < len(annex_starts) else len(lines)
        excerpt = "\n".join(lines[start:stop])[:16000]
        if has_official_comparison_heading(excerpt):
            matches.append({"heading": normalize_whitespace(lines[start]), "excerpt": excerpt})

    covered_excerpts = "\n".join(str(match.get("excerpt", "")) for match in matches)
    for index, line in enumerate(lines):
        if not has_official_comparison_heading(line):
            continue
        if line and line in covered_excerpts:
            continue
        start = max(0, index - 2)
        excerpt = "\n".join(lines[start : index + 80])[:16000]
        matches.append({"heading": normalize_whitespace(line), "excerpt": excerpt})
    return matches


def extract_official_mapping_rows(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match_index, match in enumerate(matches):
        exact_editions = bool(match.get("exact_editions"))
        for line in str(match.get("excerpt", "")).splitlines():
            cleaned = normalize_whitespace(line)
            if not cleaned or re.search(r"clause\s+in\s+iso\b.*comment\s+on\s+change", cleaned, re.I):
                continue

            correspondence = re.match(
                r"^(\d{1,2}(?:\.\d{1,3}){0,5})\s*(?:->|→|\bto\b)\s*(\d{1,2}(?:\.\d{1,3}){0,5})(?:\s+(.+))?$",
                cleaned,
                re.I,
            )
            if correspondence:
                old_id, latest_id = correspondence.group(1), correspondence.group(2)
                rows.append(
                    {
                        "old_clause_id": old_id,
                        "latest_clause_id": latest_id,
                        "change_summary": normalize_whitespace(correspondence.group(3) or "Official clause correspondence."),
                        "source_line": cleaned,
                        "source_kind": "extracted_text",
                        "exact_editions": exact_editions,
                        "explicit_correspondence": True,
                        "official_group_id": f"text-{match_index}-{len(rows)}",
                    }
                )
                continue

            comment_row = parse_official_comment_text_row(cleaned)
            if comment_row:
                comment_row.update(
                    {
                        "source_kind": "extracted_text",
                        "exact_editions": exact_editions,
                        "explicit_correspondence": False,
                        "official_group_id": f"text-{match_index}-{len(rows)}",
                    }
                )
                rows.append(comment_row)
    return rows[:500]


def parse_official_comment_text_row(line: str) -> dict[str, Any] | None:
    match = re.match(r"^(\d{1,2}(?:\.\d{1,3}){0,5})\s+(.+)$", line)
    if not match:
        return None
    clause_id, remainder = match.group(1), match.group(2)
    dash = re.search(r"\s+[—–]\s+", remainder)
    if dash:
        summary = remainder[dash.end() :]
    else:
        change_word = re.search(
            r"\b(?:adds?|added|new\s+(?:requirement|sub-?clause)|requires?|clarifies?|expanded?|deleted|removed|includes?|indicates?|lists?|focuses?|provides?|relates?|no\s+significant\s+change)\b",
            remainder,
            re.I,
        )
        if not change_word or change_word.start() == 0:
            return None
        summary = remainder[change_word.start() :]
    summary = normalize_whitespace(summary)
    if len(summary) < 8:
        return None
    return {
        "old_clause_id": "",
        "latest_clause_id": clause_id,
        "change_summary": summary,
        "source_line": line,
    }


def extract_official_mapping_rows_from_tables(
    table_records: list[dict[str, Any]],
    regulation_id: str,
    old_version: str,
    latest_version: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    header_cache: dict[str, tuple[str, int, int, int]] = {}
    old_year = edition_year(old_version)
    latest_year = edition_year(latest_version)
    for table_index, record in enumerate(table_records):
        rows = record.get("rows", [])
        if not isinstance(rows, list) or not rows:
            continue
        context = "\n".join(
            [
                str(record.get("section_context", "")),
                str(record.get("page_text", "")),
                " ".join(normalize_table_cell(cell) for row in rows[:3] if isinstance(row, list) for cell in row),
            ]
        )
        exact_editions = heading_names_uploaded_editions(context, regulation_id, old_version, latest_version)
        if not exact_editions:
            continue

        header_index, mode, old_column, latest_column, comment_column = official_table_header(
            rows, old_year, latest_year
        )
        section_key = normalize_compare_text(str(record.get("section_context", "")))
        if header_index >= 0:
            header_cache[section_key] = (mode, old_column, latest_column, comment_column)
        elif section_key in header_cache:
            mode, old_column, latest_column, comment_column = header_cache[section_key]
            header_index = -1
        if header_index < 0:
            if section_key not in header_cache:
                continue
        page_number = record.get("page_number", "")
        if mode == "comment":
            pending_ids: list[str] = []
            pending_comment = ""

            def flush_comment_group() -> None:
                nonlocal pending_ids, pending_comment
                if pending_ids and len(normalize_compare_text(pending_comment)) >= 8:
                    group_id = f"table-{table_index}-{len(output)}"
                    for latest_id in pending_ids:
                        output.append(
                            {
                                "old_clause_id": "",
                                "latest_clause_id": latest_id,
                                "change_summary": normalize_whitespace(pending_comment),
                                "source_line": f"{latest_id}: {normalize_whitespace(pending_comment)}",
                                "source_kind": "pdf_table",
                                "source_page": page_number,
                                "exact_editions": True,
                                "explicit_correspondence": False,
                                "official_group_id": group_id,
                            }
                        )
                pending_ids = []
                pending_comment = ""

            for row in rows[header_index + 1 :]:
                if not isinstance(row, list):
                    continue
                clause_cell = table_cell_at(row, latest_column)
                comment_cell = table_cell_at(row, comment_column)
                clause_ids = clause_references_from_cell(clause_cell)
                if clause_ids:
                    flush_comment_group()
                    pending_ids = clause_ids
                    pending_comment = comment_cell
                elif pending_ids and comment_cell:
                    pending_comment = normalize_whitespace(f"{pending_comment} {comment_cell}")
            flush_comment_group()
            continue

        for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 1):
            if not isinstance(row, list):
                continue
            old_ids = clause_references_from_cell(table_cell_at(row, old_column))
            latest_ids = clause_references_from_cell(table_cell_at(row, latest_column))
            for old_id, latest_id, group_size in explicit_correspondence_pairs(old_ids, latest_ids):
                output.append(
                    {
                        "old_clause_id": old_id,
                        "latest_clause_id": latest_id,
                        "change_summary": f"Official correspondence: {old_id} to {latest_id}.",
                        "source_line": f"{old_id} -> {latest_id}",
                        "source_kind": "pdf_table",
                        "source_page": page_number,
                        "exact_editions": True,
                        "explicit_correspondence": True,
                        "official_group_size": group_size,
                        "official_group_id": f"table-{table_index}-{row_index}",
                    }
                )
    return output


def official_table_header(
    rows: list[Any], old_year: str, latest_year: str
) -> tuple[int, str, int, int, int]:
    for index, row in enumerate(rows[:5]):
        if not isinstance(row, list):
            continue
        cells = [normalize_table_cell(cell).casefold() for cell in row]
        comment_column = next((i for i, cell in enumerate(cells) if "comment" in cell and "change" in cell), -1)
        latest_column = next(
            (i for i, cell in enumerate(cells) if "clause" in cell and latest_year and latest_year in cell),
            -1,
        )
        old_column = next(
            (i for i, cell in enumerate(cells) if "clause" in cell and old_year and old_year in cell),
            -1,
        )
        if comment_column >= 0 and latest_column >= 0:
            return index, "comment", -1, latest_column, comment_column
        if old_column >= 0 and latest_column >= 0 and old_column != latest_column:
            return index, "correspondence", old_column, latest_column, -1
    return -1, "", -1, -1, -1


def normalize_table_cell(value: Any) -> str:
    return "\n".join(
        normalize_whitespace(line)
        for line in str(value or "").splitlines()
        if normalize_whitespace(line)
    )


def table_cell_at(row: list[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return normalize_table_cell(row[index])


def clause_references_from_cell(value: str) -> list[str]:
    refs: list[str] = []
    for line in str(value or "").splitlines():
        match = re.match(r"^\s*(\d{1,2}(?:\.\d{1,3}){0,5})(?:\s|$)", line)
        if match and not re.fullmatch(r"\d{4}", match.group(1)):
            refs.append(match.group(1))
    if not refs:
        match = re.match(r"^\s*(\d{1,2}(?:\.\d{1,3}){0,5})(?:\s|$)", str(value or ""))
        if match:
            refs.append(match.group(1))
    return dedupe_preserve_order(refs)


def explicit_correspondence_pairs(old_ids: list[str], latest_ids: list[str]) -> list[tuple[str, str, int]]:
    if not old_ids or not latest_ids:
        return []
    group_size = max(len(old_ids), len(latest_ids))
    if len(old_ids) == len(latest_ids):
        return [(old_id, latest_id, group_size) for old_id, latest_id in zip(old_ids, latest_ids)]
    if len(old_ids) == 1:
        return [(old_ids[0], latest_id, group_size) for latest_id in latest_ids]
    if len(latest_ids) == 1:
        return [(old_id, latest_ids[0], group_size) for old_id in old_ids]
    return []


def dedupe_official_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (
            str(row.get("old_clause_id", "")),
            str(row.get("latest_clause_id", "")),
            normalize_compare_text(str(row.get("change_summary", ""))),
        )
        if not key[1] or key in seen:
            continue
        seen.add(key)
        output.append(row)
    return output[:500]


def edition_year(value: str) -> str:
    years = re.findall(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return years[-1] if years else ""


def regulation_family_token(regulation_id: str) -> str:
    match = re.search(r"\bISO[\s_-]*(\d{4,6})\b", str(regulation_id or ""), re.I)
    return f"ISO {match.group(1)}" if match else normalize_whitespace(regulation_id)


def heading_names_uploaded_editions(text: str, regulation_id: str, old_version: str, latest_version: str) -> bool:
    old_year = edition_year(old_version)
    latest_year = edition_year(latest_version)
    if not old_year or not latest_year:
        return False
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    family = regulation_family_token(regulation_id)
    iso_match = re.search(r"\bISO\s+(\d{4,6})\b", family, re.I)
    if iso_match:
        family_pattern = rf"\bISO\s*{re.escape(iso_match.group(1))}\b"
        old_named = re.search(rf"{family_pattern}\s*[:\-]?\s*{old_year}\b", normalized, re.I)
        latest_named = re.search(rf"{family_pattern}\s*[:\-]?\s*{latest_year}\b", normalized, re.I)
        return bool(old_named and latest_named)
    return bool(re.search(rf"\b{old_year}\b", normalized) and re.search(rf"\b{latest_year}\b", normalized))


def official_source_label(match: dict[str, Any]) -> str:
    excerpt_lines = [normalize_whitespace(line) for line in str(match.get("excerpt", "")).splitlines() if normalize_whitespace(line)]
    heading = normalize_whitespace(str(match.get("heading", "")))
    detail = next((line for line in excerpt_lines[:6] if has_official_comparison_heading(line)), "")
    if heading and detail and normalize_compare_text(heading) != normalize_compare_text(detail):
        return f"{heading} - {detail}"[:500]
    return (detail or heading)[:500]


def is_iso_13485_regulation(regulation_id: str, old_file: Path | None = None, latest_file: Path | None = None) -> bool:
    values = [regulation_id, old_file.name if old_file else "", latest_file.name if latest_file else ""]
    return any(re.search(r"\biso\s*13485\b|iso[_-]?13485", str(value), re.I) for value in values)


def extract_normative_clause_blocks(text: str, version: str, source_file: str) -> list[ClauseBlock]:
    lines = str(text or "").splitlines()
    start = normative_body_start(lines)
    stop = normative_body_stop(lines, start)
    blocks = extract_clause_blocks("\n".join(lines[start:stop]), version, source_file)
    return [block for block in blocks if re.fullmatch(r"\d{1,2}(?:\.\d{1,3}){0,5}", block.clause_id)]


def normative_body_start(lines: list[str]) -> int:
    candidates = [
        index
        for index, line in enumerate(lines)
        if normalize_compare_text(line) == "1 scope"
        and not any(is_annex_heading(previous) for previous in lines[max(0, index - 2) : index])
    ]
    if not candidates:
        return 0

    scored: list[tuple[int, int]] = []
    for index in candidates:
        next_heading = next(
            (
                later
                for later in range(index + 1, min(len(lines), index + 80))
                if parse_clause_heading(lines[later])
            ),
            min(len(lines), index + 80),
        )
        content_length = len(normalize_compare_text(" ".join(lines[index + 1 : next_heading])))
        scored.append((content_length, -index))
    best_score = max(scored)
    return candidates[scored.index(best_score)]


def normative_body_stop(lines: list[str], start: int) -> int:
    for index in range(max(start + 1, 0), len(lines)):
        if is_annex_heading(lines[index]) or re.match(r"^bibliography\b", normalize_whitespace(lines[index]), re.I):
            return index
    return len(lines)


def is_annex_heading(line: str) -> bool:
    cleaned = normalize_whitespace(line)
    return bool(
        re.fullmatch(r"annex\s+[a-z]", cleaned, re.I)
        or re.match(r"^annex\s+[a-z]\s*\((?:informative|normative|[^)]{0,40})\)\s*$", cleaned, re.I)
    )


def extract_iso_13485_main_body_clauses(text: str, version: str, source_file: str) -> list[ClauseBlock]:
    lines = str(text or "").splitlines()
    start = iso_13485_main_body_start(lines)
    stop = iso_13485_main_body_stop(lines, start)
    body_lines = lines[start:stop]
    blocks = extract_clause_blocks("\n".join(body_lines), version, source_file)
    return [block for block in blocks if is_valid_iso_13485_main_clause(block)]


def iso_13485_main_body_start(lines: list[str]) -> int:
    standard_markers = [
        index
        for index, line in enumerate(lines)
        if re.search(r"\binternational\s+standard\b.*\biso\s*13485\b|\biso\s*13485:\d{4}\(e\)", line, re.I)
    ]
    first_marker = min(standard_markers) if standard_markers else 0
    for index in range(first_marker, len(lines)):
        if normalize_whitespace(lines[index]).casefold() == "1 scope":
            return index
    for index, line in enumerate(lines):
        if normalize_whitespace(line).casefold() == "1 scope":
            return index
    return 0


def iso_13485_main_body_stop(lines: list[str], start: int) -> int:
    for index in range(max(start, 0), len(lines)):
        if is_annex_heading(lines[index]) or re.match(r"^bibliography\b", normalize_whitespace(lines[index]), re.I):
            return index
    return len(lines)


def extract_iso_13485_annex_a_mapping_reference(text: str) -> dict[str, Any]:
    lines = str(text or "").splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(r"^annex\s+a\b", normalize_whitespace(line), re.I)
        ),
        -1,
    )
    if start < 0:
        return {"detected": False, "mapping_rows": []}
    stop = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^(annex\s+b\b|bibliography\b)", normalize_whitespace(lines[index]), re.I):
            stop = index
            break
    rows = []
    for line in lines[start:stop]:
        match = re.match(r"^([0-8](?:\.\d{1,2}){0,4})\s+(.{2,220})$", normalize_whitespace(line))
        if not match:
            continue
        clause_id = match.group(1)
        source_line = normalize_whitespace(line)
        if is_toc_like_clause_title(source_line):
            continue
        rows.append({"latest_clause_id": clause_id, "source_line": source_line})
    return {"detected": True, "mapping_rows": rows}


def is_valid_iso_13485_main_clause(block: ClauseBlock) -> bool:
    if not re.fullmatch(r"[1-8](?:\.\d{1,2}){0,4}", block.clause_id):
        return False
    if is_toc_like_clause_title(block.clause_title):
        return False
    return len(normalize_compare_text(block.clause_text)) >= 40


def is_toc_like_clause_title(title: str) -> bool:
    cleaned = normalize_whitespace(title)
    if re.search(r"\.{4,}", cleaned):
        return True
    if re.search(r"\s\d{1,3}$", cleaned) and not re.search(r"\bISO\s+\d+$", cleaned, re.I):
        return True
    return False


def is_distinctive_clause_title(title: str) -> bool:
    normalized = normalize_compare_text(title)
    return normalized not in {"general", "application", "planning", "feedback"}


def extract_clause_blocks(text: str, version: str, source_file: str) -> list[ClauseBlock]:
    blocks: list[ClauseBlock] = []
    current_id = "front_matter"
    current_title = "Front matter"
    current_lines: list[str] = []
    for line in str(text or "").splitlines():
        heading = parse_clause_heading(line)
        if heading:
            if current_lines:
                blocks.append(
                    ClauseBlock(current_id, current_title, "\n".join(current_lines), source_version=version, source_file=source_file)
                )
            current_id, current_title = heading
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        blocks.append(ClauseBlock(current_id, current_title, "\n".join(current_lines), source_version=version, source_file=source_file))
    return [block for block in blocks if block.clause_id != "front_matter" or meaningful_text(block.clause_text)]


def parse_clause_heading(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    match = re.match(r"^(\d{1,2}(?:\.\d{1,3}){0,5})\s+(.{2,160})$", stripped)
    if not match:
        return None
    clause_id = match.group(1)
    if re.fullmatch(r"\d{4}", clause_id):
        return None
    title = normalize_whitespace(match.group(2))
    if len(title.split()) > 18:
        return None
    return clause_id, title


def build_clause_mappings(old_clauses: list[ClauseBlock], latest_clauses: list[ClauseBlock], official: dict[str, Any]) -> list[dict[str, Any]]:
    old_by_id = {item.clause_id: item for item in old_clauses}
    latest_by_id = {item.clause_id: item for item in latest_clauses}
    used_old: set[str] = set()
    used_latest: set[str] = set()
    mappings: list[dict[str, Any]] = []

    official_rows = official.get("mapping_rows", []) if official.get("usable_as_primary") else []
    for row in official_rows:
        old_id = row.get("old_clause_id", "")
        latest_id = row.get("latest_clause_id", "")
        if old_id in old_by_id and latest_id in latest_by_id and old_id not in used_old and latest_id not in used_latest:
            mappings.append(mapping_row(old_id, latest_id, "official_comparison_material", old_by_id[old_id], latest_by_id[latest_id]))
            used_old.add(old_id)
            used_latest.add(latest_id)

    for latest in latest_clauses:
        if latest.clause_id in used_latest:
            continue
        old = old_by_id.get(latest.clause_id)
        if old and old.clause_id not in used_old:
            title_score = similarity(old.clause_title, latest.clause_title)
            if title_score >= 0.75:
                mappings.append(mapping_row(old.clause_id, latest.clause_id, "exact_clause_number", old, latest))
                used_old.add(old.clause_id)
                used_latest.add(latest.clause_id)

    for latest in latest_clauses:
        if latest.clause_id in used_latest:
            continue
        candidates = []
        for old in old_clauses:
            if old.clause_id in used_old:
                continue
            title_score = similarity(old.clause_title, latest.clause_title)
            text_score = similarity(normalize_compare_text(old.clause_text), normalize_compare_text(latest.clause_text))
            score = max(title_score, text_score)
            candidates.append((score, title_score, text_score, old))
        if not candidates:
            continue
        score, title_score, text_score, old = max(candidates, key=lambda item: item[0])
        if title_score >= 0.72 or text_score >= 0.65:
            method = "title_similarity" if title_score >= text_score else "semantic_text_similarity"
            mappings.append(mapping_row(old.clause_id, latest.clause_id, method, old, latest, score))
            used_old.add(old.clause_id)
            used_latest.add(latest.clause_id)

    for latest in latest_clauses:
        if latest.clause_id not in used_latest:
            mappings.append(mapping_row("", latest.clause_id, "unmapped_latest_clause", None, latest))
    for old in old_clauses:
        if old.clause_id not in used_old:
            mappings.append(mapping_row(old.clause_id, "", "unmapped_old_clause", old, None))
    return mappings


def build_iso_13485_clause_mappings(
    old_clauses: list[ClauseBlock],
    latest_clauses: list[ClauseBlock],
    official: dict[str, Any],
) -> list[dict[str, Any]]:
    old_by_id = {item.clause_id: item for item in old_clauses}
    latest_by_id = {item.clause_id: item for item in latest_clauses}
    used_old: set[str] = set()
    used_latest: set[str] = set()
    mappings: list[dict[str, Any]] = []

    official_rows = official.get("mapping_rows", []) if official.get("usable_as_primary") else []
    for row in official_rows:
        old_id = row.get("old_clause_id", "")
        latest_id = row.get("latest_clause_id", "")
        if old_id in old_by_id and latest_id in latest_by_id and old_id not in used_old and latest_id not in used_latest:
            mappings.append(mapping_row(old_id, latest_id, "official_comparison_material", old_by_id[old_id], latest_by_id[latest_id]))
            used_old.add(old_id)
            used_latest.add(latest_id)

    for old_id, latest_id in ISO_13485_KNOWN_RENUMBERINGS.items():
        if old_id in old_by_id and latest_id in latest_by_id and old_id not in used_old and latest_id not in used_latest:
            mappings.append(mapping_row(old_id, latest_id, "known_iso_13485_renumbering", old_by_id[old_id], latest_by_id[latest_id]))
            used_old.add(old_id)
            used_latest.add(latest_id)

    for latest in latest_clauses:
        if latest.clause_id in used_latest or latest.clause_id in ISO_13485_NEW_OR_SEPARATED_LATEST_CLAUSES:
            continue
        exact_title_old = next(
            (
                old
                for old in old_clauses
                if old.clause_id not in used_old
                and is_distinctive_clause_title(old.clause_title)
                and normalize_compare_text(old.clause_title) == normalize_compare_text(latest.clause_title)
            ),
            None,
        )
        if exact_title_old:
            mappings.append(mapping_row(exact_title_old.clause_id, latest.clause_id, "exact_title_match", exact_title_old, latest))
            used_old.add(exact_title_old.clause_id)
            used_latest.add(latest.clause_id)

    for latest in latest_clauses:
        if latest.clause_id in used_latest or latest.clause_id in ISO_13485_NEW_OR_SEPARATED_LATEST_CLAUSES:
            continue
        old = old_by_id.get(latest.clause_id)
        if old and old.clause_id not in used_old:
            title_score = similarity(old.clause_title, latest.clause_title)
            if title_score >= 0.75:
                mappings.append(mapping_row(old.clause_id, latest.clause_id, "exact_clause_number", old, latest))
                used_old.add(old.clause_id)
                used_latest.add(latest.clause_id)

    for latest in latest_clauses:
        if latest.clause_id in used_latest or latest.clause_id in ISO_13485_NEW_OR_SEPARATED_LATEST_CLAUSES:
            continue
        candidates = []
        for old in old_clauses:
            if old.clause_id in used_old:
                continue
            if not (is_distinctive_clause_title(old.clause_title) and is_distinctive_clause_title(latest.clause_title)):
                continue
            title_score = similarity(old.clause_title, latest.clause_title)
            text_score = similarity(normalize_compare_text(old.clause_text), normalize_compare_text(latest.clause_text))
            candidates.append((title_score, text_score, old))
        if not candidates:
            continue
        title_score, text_score, old = max(candidates, key=lambda item: (item[0], item[1]))
        if title_score >= 0.72:
            mappings.append(mapping_row(old.clause_id, latest.clause_id, "title_similarity", old, latest))
            used_old.add(old.clause_id)
            used_latest.add(latest.clause_id)

    for latest in latest_clauses:
        if latest.clause_id in used_latest or latest.clause_id in ISO_13485_NEW_OR_SEPARATED_LATEST_CLAUSES:
            continue
        candidates = []
        for old in old_clauses:
            if old.clause_id in used_old:
                continue
            text_score = similarity(normalize_compare_text(old.clause_text), normalize_compare_text(latest.clause_text))
            candidates.append((text_score, old))
        if not candidates:
            continue
        text_score, old = max(candidates, key=lambda item: item[0])
        if text_score >= 0.70:
            mappings.append(mapping_row(old.clause_id, latest.clause_id, "semantic_text_similarity", old, latest))
            used_old.add(old.clause_id)
            used_latest.add(latest.clause_id)

    for latest in latest_clauses:
        if latest.clause_id not in used_latest:
            mappings.append(mapping_row("", latest.clause_id, "unmapped_latest_clause", None, latest))
    for old in old_clauses:
        if old.clause_id not in used_old:
            mappings.append(mapping_row(old.clause_id, "", "unmapped_old_clause", old, None))
    return mappings


def attach_official_evidence(mappings: list[dict[str, Any]], official: dict[str, Any]) -> None:
    if not official.get("usable_as_primary"):
        return
    rows = official.get("mapping_rows", [])
    for mapping in mappings:
        old_id = str(mapping.get("old_clause_id", ""))
        latest_id = str(mapping.get("latest_clause_id", ""))
        evidence = next(
            (
                row
                for row in rows
                if str(row.get("latest_clause_id", "")) == latest_id
                and (not row.get("old_clause_id") or str(row.get("old_clause_id")) == old_id)
            ),
            None,
        )
        if evidence:
            mapping["official_evidence"] = evidence


def mapping_row(
    old_id: str,
    latest_id: str,
    method: str,
    old: ClauseBlock | None,
    latest: ClauseBlock | None,
    score: float | None = None,
) -> dict[str, Any]:
    title_score = similarity(old.clause_title if old else "", latest.clause_title if latest else "")
    text_score = similarity(normalize_compare_text(old.clause_text if old else ""), normalize_compare_text(latest.clause_text if latest else ""))
    if score is None:
        score = max(title_score, text_score)
    return {
        "old_clause_id": old_id,
        "latest_clause_id": latest_id,
        "mapping_method": method,
        "similarity_score": round(float(score), 4),
        "title_similarity": round(float(title_score), 4),
        "text_similarity": round(float(text_score), 4),
    }


def classify_mapping(
    mapping: dict[str, Any],
    old_clauses: list[ClauseBlock],
    latest_clauses: list[ClauseBlock],
    regulation_id: str,
    old_version: str,
    latest_version: str,
) -> dict[str, Any]:
    old = next((item for item in old_clauses if item.clause_id == mapping.get("old_clause_id")), None)
    latest = next((item for item in latest_clauses if item.clause_id == mapping.get("latest_clause_id")), None)
    old_units = requirement_units(old.clause_text if old else "")
    latest_units = requirement_units(latest.clause_text if latest else "")
    added = unmatched_units(latest_units, old_units)
    removed = unmatched_units(old_units, latest_units)
    changed = changed_units(old_units, latest_units)
    classification = classify_change(mapping, old, latest, added, removed, changed)
    official_evidence = mapping.get("official_evidence", {})
    official_summary = normalize_whitespace(str(official_evidence.get("change_summary", "")))
    official_change_kind = classify_official_change_summary(official_summary)
    direct_mapping_validated = validated_two_sided_mapping(mapping, old, latest)
    direct_change_kind = direct_change_evidence_kind(classification)
    evidence_conflict = bool(
        official_evidence
        and (
            (official_change_kind == "substantive" and direct_change_kind == "non_substantive")
            or (official_change_kind == "non_substantive" and direct_change_kind == "substantive")
        )
    )
    accepted_for_drafting = accepted_change_evidence(
        classification,
        mapping,
        old,
        latest,
        direct_mapping_validated,
        official_change_kind,
        evidence_conflict,
    )
    evidence_method = OFFICIAL_COMPARISON_METHOD if official_evidence else DIRECT_COMPARISON_METHOD
    needs_human_confirmation = bool(
        evidence_conflict
        or classification in {"uncertain_need_confirmation", "newly_added_clause", "deleted_clause"}
        or (classification in ACCEPTED_SUBSTANTIVE_CLASSES and not accepted_for_drafting)
    )
    combined_text = "\n".join([old.clause_text if old else "", latest.clause_text if latest else ""])
    topics, keywords = extract_topics_from_text(combined_text)
    limitations = limitations_for_classification(classification, mapping)
    if evidence_conflict:
        limitations.append("Official comparison evidence conflicts with the extracted normative clause comparison.")
    return {
        "regulation_id": regulation_id,
        "old_version": old_version,
        "latest_version": latest_version,
        "old_clause_id": mapping.get("old_clause_id", ""),
        "old_clause_title": old.clause_title if old else "",
        "latest_clause_id": mapping.get("latest_clause_id", ""),
        "latest_clause_title": latest.clause_title if latest else "",
        "mapping_method": mapping.get("mapping_method", ""),
        "similarity_score": mapping.get("similarity_score", 0),
        "title_similarity": mapping.get("title_similarity", 0),
        "text_similarity": mapping.get("text_similarity", 0),
        "change_classification": classification,
        "added_requirement_units": added,
        "removed_requirement_units": removed,
        "changed_requirement_units": changed,
        "changed_terms": changed_terms(old.clause_text if old else "", latest.clause_text if latest else ""),
        "old_clause_text": old.clause_text if old else "",
        "latest_clause_text": latest.clause_text if latest else "",
        "affected_qms_topics": topics,
        "search_keywords": keywords,
        "impact_level": impact_level(classification, len(added), len(removed), len(changed)),
        "evidence_method": evidence_method,
        "evidence_source": official_evidence.get("source_kind", "Uploaded normative clause text"),
        "official_change_summary": official_summary,
        "official_change_kind": official_change_kind,
        "official_source_page": official_evidence.get("source_page", ""),
        "official_correspondence_explicit": bool(official_evidence.get("explicit_correspondence")),
        "direct_mapping_validated": direct_mapping_validated,
        "evidence_conflict": evidence_conflict,
        "accepted_for_drafting": accepted_for_drafting,
        "needs_human_confirmation": needs_human_confirmation,
        "extraction_warning": "",
        "evidence_preview": evidence_preview(old, latest),
        "limitations": limitations,
    }


def classify_change(
    mapping: dict[str, Any],
    old: ClauseBlock | None,
    latest: ClauseBlock | None,
    added: list[str],
    removed: list[str],
    changed: list[str],
) -> str:
    if old is None and latest is not None:
        return "newly_added_clause"
    if old is not None and latest is None:
        return "deleted_clause"
    if old is None or latest is None:
        return "uncertain_need_confirmation"
    official_evidence = mapping.get("official_evidence", {})
    if official_evidence.get("explicit_correspondence") and int(official_evidence.get("official_group_size", 1) or 1) > 1:
        return "split_or_merged_clause"

    same_id = old.clause_id == latest.clause_id
    old_norm = normalize_compare_text(old.clause_text)
    latest_norm = normalize_compare_text(latest.clause_text)
    text_score = similarity(old_norm, latest_norm)
    if old_norm == latest_norm:
        return "unchanged" if same_id else "renumbered_only"
    if not same_id and text_score >= 0.93 and not added and not removed and not changed:
        return "renumbered_only"
    if not same_id and (added or removed or changed or text_score < 0.93):
        return "renumbered_and_modified"
    if added and removed:
        return "modified_requirement"
    if added:
        return "expanded_requirement"
    if removed:
        return "reduced_or_deleted_requirement"
    if changed:
        return "modified_requirement"
    if text_score >= 0.88:
        return "editorial_change_only"
    return "uncertain_need_confirmation"


def classify_official_change_summary(summary: str) -> str:
    normalized = normalize_compare_text(summary)
    if not normalized:
        return "unknown"
    if re.search(r"\b(no significant change|no substantive change|editorial only|renumbered only|wording only)\b", normalized):
        return "non_substantive"
    if re.search(
        r"\b(new requirement|new sub clause|added requirement|adds requirement|requires|requirement added|expanded|deleted|removed|more detail|additional detail)\b",
        normalized,
    ) or re.search(r"\b(?:added|adds)\b.{0,60}\brequirement\b", normalized):
        return "substantive"
    return "unknown"


def direct_change_evidence_kind(classification: str) -> str:
    if classification in ACCEPTED_SUBSTANTIVE_CLASSES:
        return "substantive"
    if classification in {"unchanged", EDITORIAL_CLASS, "renumbered_only"}:
        return "non_substantive"
    return "unknown"


def validated_two_sided_mapping(
    mapping: dict[str, Any], old: ClauseBlock | None, latest: ClauseBlock | None
) -> bool:
    if old is None or latest is None:
        return False
    method = str(mapping.get("mapping_method", ""))
    if method == "exact_clause_number":
        return float(mapping.get("title_similarity", 0) or 0) >= 0.75
    if method in {
        "exact_title_match",
        "known_iso_13485_renumbering",
        "official_comparison_material",
    }:
        return True
    if method == "title_similarity":
        return float(mapping.get("title_similarity", 0) or 0) >= 0.85
    if method == "semantic_text_similarity":
        return float(mapping.get("text_similarity", 0) or 0) >= 0.85
    return False


def accepted_change_evidence(
    classification: str,
    mapping: dict[str, Any],
    old: ClauseBlock | None,
    latest: ClauseBlock | None,
    direct_mapping_validated: bool,
    official_change_kind: str,
    evidence_conflict: bool,
) -> bool:
    if evidence_conflict or old is None or latest is None:
        return False
    if classification not in ACCEPTED_SUBSTANTIVE_CLASSES:
        return False
    if classification == "split_or_merged_clause":
        evidence = mapping.get("official_evidence", {})
        return bool(evidence.get("explicit_correspondence"))
    if mapping.get("official_evidence"):
        return official_change_kind != "non_substantive" and direct_mapping_validated
    return direct_mapping_validated


def evidence_id_for_row(row: dict[str, Any]) -> str:
    payload = {
        "regulation_id": normalize_compare_text(str(row.get("regulation_id", ""))),
        "old_version": normalize_compare_text(str(row.get("old_version", ""))),
        "latest_version": normalize_compare_text(str(row.get("latest_version", ""))),
        "old_clause_id": str(row.get("old_clause_id", "")),
        "latest_clause_id": str(row.get("latest_clause_id", "")),
        "classification": str(row.get("change_classification", "")),
        "evidence_method": str(row.get("evidence_method", "")),
        "official_change_summary": normalize_compare_text(str(row.get("official_change_summary", ""))),
        "old_clause_text": normalize_compare_text(str(row.get("old_clause_text", ""))),
        "latest_clause_text": normalize_compare_text(str(row.get("latest_clause_text", ""))),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"ace-{digest[:20]}"


def requirement_units(text: str) -> list[str]:
    units: list[str] = []
    for part in re.split(r"(?<=[.;:])\s+|\n+", str(text or "")):
        cleaned = normalize_whitespace(part)
        if len(cleaned) < 12:
            continue
        lowered = cleaned.casefold()
        if any(signal in lowered for signal in REQUIREMENT_SIGNALS):
            units.append(cleaned)
    if units:
        return dedupe_preserve_order(units)
    return dedupe_preserve_order([normalize_whitespace(part) for part in re.split(r"\n+", text) if len(part.strip()) >= 30])


def unmatched_units(source: list[str], target: list[str]) -> list[str]:
    result: list[str] = []
    for unit in source:
        unit_norm = normalize_compare_text(unit)
        if any(normalize_compare_text(other) in unit_norm and len(unit_norm) - len(normalize_compare_text(other)) > 15 for other in target):
            result.append(unit)
            continue
        best = max((similarity(unit, other) for other in target), default=0.0)
        if best < 0.78:
            result.append(unit)
    return result[:20]


def changed_units(old_units: list[str], latest_units: list[str]) -> list[str]:
    result: list[str] = []
    for latest in latest_units:
        scores = [(similarity(latest, old), old) for old in old_units]
        if not scores:
            continue
        best_score, old = max(scores, key=lambda item: item[0])
        if 0.55 <= best_score < 0.88:
            result.append(f"Old: {old} -> Latest: {latest}")
    return result[:20]


def aggregate_clause_comparisons(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (str(row.get("old_clause_id", "")), str(row.get("latest_clause_id", "")))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    output: list[dict[str, Any]] = []
    for key in order:
        items = grouped[key]
        best = max(items, key=lambda item: CLASSIFICATION_STRENGTH.get(str(item.get("change_classification", "")), 50))
        merged = dict(best)
        merged["added_requirement_units"] = merge_list_field(items, "added_requirement_units")
        merged["removed_requirement_units"] = merge_list_field(items, "removed_requirement_units")
        merged["changed_requirement_units"] = merge_list_field(items, "changed_requirement_units")
        merged["changed_terms"] = merge_list_field(items, "changed_terms")
        merged["affected_qms_topics"] = merge_list_field(items, "affected_qms_topics")
        merged["search_keywords"] = merge_list_field(items, "search_keywords")
        merged["limitations"] = merge_list_field(items, "limitations")
        merged["evidence_preview"] = "\n".join(dedupe_preserve_order([str(item.get("evidence_preview", "")) for item in items if item.get("evidence_preview")]))[:1500]
        merged["extraction_warning"] = " ".join(dedupe_preserve_order([str(item.get("extraction_warning", "")) for item in items if item.get("extraction_warning")]))
        merged["evidence_conflict"] = any(bool(item.get("evidence_conflict")) for item in items)
        merged["accepted_for_drafting"] = bool(
            not merged["evidence_conflict"] and any(bool(item.get("accepted_for_drafting")) for item in items)
        )
        merged["needs_human_confirmation"] = bool(
            merged["evidence_conflict"] or any(bool(item.get("needs_human_confirmation")) for item in items)
        )
        classifications = sorted({str(item.get("change_classification", "")) for item in items if item.get("change_classification")})
        if len(classifications) > 1:
            merged["pre_aggregation_classifications"] = classifications
        output.append(merged)
    return output


def merge_list_field(rows: list[dict[str, Any]], field: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        value = row.get(field, [])
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    return dedupe_preserve_order(values)


def classification_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in sorted(SUBSTANTIVE_CLASSES | {EDITORIAL_CLASS, "unchanged", "renumbered_only"})}
    for row in results:
        classification = str(row.get("change_classification", "uncertain_need_confirmation"))
        counts[classification] = counts.get(classification, 0) + 1
    counts["mapped_clauses"] = len(results)
    counts["substantive_or_uncertain"] = sum(counts.get(name, 0) for name in SUBSTANTIVE_CLASSES)
    return counts


def write_comparison_audit(output_dir: Path, comparison: dict[str, Any]) -> None:
    if comparison.get("comparison_validation_failed"):
        failures = "; ".join(str(item) for item in comparison.get("validation_failures", []))
        raise RuntimeError(f"Comparison validation failed; audit export blocked. {failures}")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "regulation_clause_index_old.json").write_text(
        json.dumps(comparison.get("old_clause_index", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "regulation_clause_index_latest.json").write_text(
        json.dumps(comparison.get("latest_clause_index", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "old_body_clause_index.json").write_text(
        json.dumps(comparison.get("old_body_clause_index", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "latest_body_clause_index.json").write_text(
        json.dumps(comparison.get("latest_body_clause_index", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "annex_a_mapping_reference.json").write_text(
        json.dumps(comparison.get("annex_a_mapping_reference", {}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "official_comparison.json").write_text(
        json.dumps(comparison.get("official_comparison", {}), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "accepted_changes.json").write_text(
        json.dumps(comparison.get("accepted_changes", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rows = comparison.get("clause_comparisons", [])
    mapping_columns = [
        "regulation_id",
        "old_version",
        "latest_version",
        "old_clause_id",
        "old_clause_title",
        "latest_clause_id",
        "latest_clause_title",
        "mapping_method",
        "similarity_score",
        "title_similarity",
        "text_similarity",
        "change_classification",
    ]
    result_columns = [
        "regulation_id",
        "old_version",
        "latest_version",
        "old_clause_id",
        "old_clause_title",
        "latest_clause_id",
        "latest_clause_title",
        "mapping_method",
        "similarity_score",
        "title_similarity",
        "text_similarity",
        "change_classification",
        "evidence_id",
        "evidence_method",
        "evidence_source",
        "official_change_summary",
        "direct_mapping_validated",
        "evidence_conflict",
        "accepted_for_drafting",
        "added_requirement_count",
        "removed_requirement_count",
        "changed_requirement_count",
        "affected_qms_topics",
        "impact_level",
        "needs_human_confirmation",
        "old_clause_text",
        "latest_clause_text",
        "extraction_warning",
        "evidence_preview",
        "limitations",
    ]
    mapping_rows = [{column: csv_value(row.get(column, "")) for column in mapping_columns} for row in rows]
    result_rows = []
    for row in rows:
        output = {column: csv_value(row.get(column, "")) for column in result_columns}
        output["added_requirement_count"] = len(row.get("added_requirement_units", []))
        output["removed_requirement_count"] = len(row.get("removed_requirement_units", []))
        output["changed_requirement_count"] = len(row.get("changed_requirement_units", []))
        result_rows.append(output)
    write_csv(output_dir / "regulation_clause_mapping.csv", mapping_columns, mapping_rows)
    write_csv(output_dir / "clause_comparison_results.csv", result_columns, result_rows)
    summary = {
        "regulation_id": comparison.get("regulation_id", ""),
        "old_version": comparison.get("old_version", ""),
        "latest_version": comparison.get("latest_version", ""),
        "selected_method": comparison.get("selected_method", ""),
        "evidence_source": comparison.get("evidence_source", ""),
        "official_comparison_detected": comparison.get("official_comparison", {}).get("detected", False),
        "official_comparison_usable": comparison.get("official_comparison", {}).get("usable_as_primary", False),
        "accepted_change_count": len(comparison.get("accepted_changes", [])),
        "classification_counts": comparison.get("classification_counts", {}),
        "limitations": comparison.get("limitations", []),
    }
    with pd.ExcelWriter(output_dir / "amendment_profile_summary.xlsx") as writer:
        pd.DataFrame([summary]).to_excel(writer, index=False, sheet_name="summary")
        pd.DataFrame(result_rows).to_excel(writer, index=False, sheet_name="clause_results")


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def csv_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def extract_topics_and_keywords(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    text = "\n".join(
        [
            " ".join(row.get("affected_qms_topics", []))
            + " "
            + " ".join(row.get("search_keywords", []))
            + " "
            + str(row.get("evidence_preview", ""))
            for row in rows
        ]
    )
    return extract_topics_from_text(text)


def extract_topics_from_text(text: str) -> tuple[list[str], list[str]]:
    lowered = str(text or "").casefold()
    topics: list[str] = []
    keywords: list[str] = []
    for topic, terms, keys in QMS_TOPIC_RULES:
        if any(term.casefold() in lowered for term in terms):
            topics.append(topic)
            keywords.extend(keys)
    return dedupe_preserve_order(topics), dedupe_preserve_order(keywords)


def comparison_quality_warnings(
    results: list[dict[str, Any]],
    old_clauses: list[ClauseBlock],
    latest_clauses: list[ClauseBlock],
    is_iso_13485: bool,
    raw_results: list[dict[str, Any]] | None = None,
) -> list[str]:
    warnings: list[str] = []
    if is_iso_13485:
        latest_bad_ids = [block.clause_id for block in latest_clauses if re.match(r"^(9|10)(\.|$)", block.clause_id)]
        if latest_bad_ids:
            warnings.append(f"ISO 13485 latest extraction contains Annex B-like clause IDs: {', '.join(latest_bad_ids[:10])}.")
        toc_titles = [
            block.clause_id
            for block in old_clauses + latest_clauses
            if is_toc_like_clause_title(block.clause_title)
        ]
        if toc_titles:
            warnings.append(f"Clause extraction contains TOC-like titles: {', '.join(toc_titles[:10])}.")
        if len(results) > 150:
            warnings.append(f"ISO 13485 comparison produced {len(results)} rows; expected fewer than 150.")

    bad_exact = [
        f"{row.get('old_clause_id')}->{row.get('latest_clause_id')}"
        for row in results
        if row.get("mapping_method") == "exact_clause_number"
        and similarity(str(row.get("old_clause_title", "")), str(row.get("latest_clause_title", ""))) < 0.75
    ]
    if bad_exact:
        warnings.append(f"Exact clause-number mapping has low title similarity: {', '.join(bad_exact[:10])}.")

    deleted_or_added = sum(
        1
        for row in results
        if row.get("change_classification") in {"deleted_clause", "newly_added_clause"}
    )
    if results and deleted_or_added / len(results) > 0.45 and deleted_or_added > 20:
        warnings.append(f"Deleted/newly-added clause count is unusually high: {deleted_or_added}.")
    conflicts = conflicting_pair_classifications(raw_results or [])
    if conflicts:
        warnings.append(f"Duplicate clause-pair classifications were aggregated: {', '.join(conflicts[:10])}.")
    return warnings


def comparison_validation_failures(
    results: list[dict[str, Any]],
    old_clauses: list[ClauseBlock],
    latest_clauses: list[ClauseBlock],
    is_iso_13485: bool,
) -> list[str]:
    if not is_iso_13485:
        return []
    failures: list[str] = []
    failures.extend(duplicate_clause_title_failures(old_clauses, "old"))
    failures.extend(duplicate_clause_title_failures(latest_clauses, "latest"))
    annex_rows = [
        block.clause_id
        for block in old_clauses + latest_clauses
        if re.search(r"table\s+[ab]\.\d|clause\s+in\s+iso\s+13485|comment\s+on\s+change|comparison\s+of\s+content", block.clause_text, re.I)
    ]
    if annex_rows:
        failures.append(f"Annex/comparison-table content appears in ISO 13485 body clause index: {', '.join(annex_rows[:10])}.")
    latest_by_id = {block.clause_id: block for block in latest_clauses}
    for clause_id, expected_title in ISO_13485_2016_KNOWN_BODY_TITLES.items():
        block = latest_by_id.get(clause_id)
        if block and normalize_compare_text(block.clause_title) != normalize_compare_text(expected_title):
            failures.append(f"ISO 13485:2016 clause {clause_id} title is {block.clause_title!r}, expected {expected_title!r}.")
    if len(results) > 150:
        failures.append(f"ISO 13485 comparison produced {len(results)} rows after aggregation; expected fewer than 150.")
    return failures


def duplicate_clause_title_failures(clauses: list[ClauseBlock], label: str) -> list[str]:
    titles_by_id: dict[str, set[str]] = {}
    for block in clauses:
        titles_by_id.setdefault(block.clause_id, set()).add(normalize_compare_text(block.clause_title))
    return [
        f"ISO 13485 {label} clause {clause_id} has multiple body titles."
        for clause_id, titles in sorted(titles_by_id.items())
        if len(titles) > 1
    ]


def conflicting_pair_classifications(rows: list[dict[str, Any]]) -> list[str]:
    classifications_by_pair: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (str(row.get("old_clause_id", "")), str(row.get("latest_clause_id", "")))
        classifications_by_pair.setdefault(key, set()).add(str(row.get("change_classification", "")))
    return [
        f"{old_id}->{latest_id}"
        for (old_id, latest_id), classifications in classifications_by_pair.items()
        if len(classifications) > 1
    ]


def apply_extraction_warnings(results: list[dict[str, Any]], warnings: list[str]) -> None:
    global_warning = " ".join(warnings)
    for row in results:
        row_warnings: list[str] = []
        if (
            row.get("mapping_method") == "exact_clause_number"
            and similarity(str(row.get("old_clause_title", "")), str(row.get("latest_clause_title", ""))) < 0.75
        ):
            row_warnings.append("Exact clause-number mapping has low title similarity.")
        if global_warning:
            row_warnings.append(global_warning)
        row["extraction_warning"] = " ".join(row_warnings)


def comparison_limitations(
    old_text: str,
    latest_text: str,
    old_clauses: list[ClauseBlock],
    latest_clauses: list[ClauseBlock],
    official: dict[str, Any],
    extraction_warnings: list[str] | None = None,
) -> list[str]:
    limitations = ["Human QA/RA confirmation is required before controlled document amendment."]
    if not old_text or not latest_text:
        limitations.append("Text extraction failed for one or both uploaded regulation files.")
    if not old_clauses or not latest_clauses:
        limitations.append("Clause segmentation was incomplete; unmapped results may need manual confirmation.")
    if not official.get("detected"):
        limitations.append("No official comparison annex or revision summary was detected in extracted text.")
    elif not official.get("usable_as_primary"):
        limitations.append(str(official.get("rejection_reason") or "Official comparison material was not valid for the uploaded editions."))
    limitations.extend(extraction_warnings or [])
    return limitations


def limitations_for_classification(classification: str, mapping: dict[str, Any]) -> list[str]:
    limitations = ["Human QA/RA confirmation required."]
    if classification == "uncertain_need_confirmation":
        limitations.append("Mapping or extracted text was not clear enough for a confident deterministic classification.")
    if mapping.get("mapping_method") in {"title_similarity", "semantic_text_similarity"}:
        limitations.append("Clause was mapped by similarity rather than an explicit official mapping.")
    return limitations


def impact_level(classification: str, added_count: int, removed_count: int, changed_count: int) -> str:
    if classification in {"expanded_requirement", "reduced_or_deleted_requirement", "renumbered_and_modified", "split_or_merged_clause"}:
        return "High"
    if classification in {"modified_requirement", "newly_added_clause", "deleted_clause", "uncertain_need_confirmation"}:
        return "Medium"
    if added_count or removed_count or changed_count:
        return "Medium"
    return "Low"


def changed_terms(old_text: str, latest_text: str) -> list[str]:
    terms = ["shall", "documented", "record", "risk", "validation", "supplier", "software", "complaint", "reporting"]
    changed = []
    old_lower = old_text.casefold()
    latest_lower = latest_text.casefold()
    for term in terms:
        if old_lower.count(term) != latest_lower.count(term):
            changed.append(term)
    return changed


def evidence_preview(old: ClauseBlock | None, latest: ClauseBlock | None) -> str:
    parts = []
    if old:
        parts.append(f"Old {old.clause_id}: {old.clause_text[:500]}")
    if latest:
        parts.append(f"Latest {latest.clause_id}: {latest.clause_text[:500]}")
    return "\n".join(parts)


def meaningful_text(text: str) -> bool:
    return len(normalize_compare_text(text)) >= 80


def normalize_compare_text(text: str) -> str:
    text = str(text or "").casefold()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def similarity(left: str, right: str) -> float:
    left = normalize_whitespace(left)
    right = normalize_whitespace(right)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        value = normalize_whitespace(value)
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output
