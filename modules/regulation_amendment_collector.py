from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import pandas as pd
import requests

from .regulation_version_comparator import (
    compare_regulation_versions,
    extract_regulation_text,
    write_comparison_audit,
)

PREFERRED_SHEET = "原始总表"
PROMPT_VERSION = "excel_fallback_amendment_profile_v2"
REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; QMSRegulationAmendmentProfiler/1.0)"
SEARCH_URL = "https://duckduckgo.com/html/?q={query}"
ISO_ALGOLIA_URL = "https://JCL49WV5AR-dsn.algolia.net/1/indexes/*/queries"
ISO_ALGOLIA_HEADERS = {
    "x-algolia-application-id": "JCL49WV5AR",
    "x-algolia-api-key": "MzcxYjJlODU3ZmEwYmRhZTc0NTZlODNlZmUwYzVjNDRiZDEzMzRjMjYwNTAwODU3YmIzNjEwZmNjNDFlOTBjYXJlc3RyaWN0SW5kaWNlcz1QUk9EX2lzb29yZ19lbiUyQ1BST0RfaXNvb3JnX2VuX2F1dG9jb21wbGV0ZQ==",
    "content-type": "application/json",
}

MASTER_COLUMNS = [
    "row_index",
    "original_standard_code",
    "original_year",
    "latest_standard_code",
    "latest_year",
    "status",
    "source_mode",
    "confidence",
    "exact_clause_verified",
    "exact_amendment_known",
    "full_text_available",
    "old_version_available",
    "latest_version_available",
    "amendment_summary_used",
    "affected_qms_topics",
    "search_keywords",
    "needs_human_confirmation",
    "limitations",
    "profile_path",
]
LOG_COLUMNS = ["timestamp", "row_index", "regulation_id", "source_mode", "cache_status", "message"]
ERROR_COLUMNS = ["row_index", "original_standard_code", "latest_standard_code", "error_type", "error_message", "timestamp"]
SUMMARY_SOURCE_MODES = {"official_summary", "free_online_summary"}

TOPIC_RULES: list[tuple[str, list[str], list[str]]] = [
    ("risk management", ["风险", "risk", "iso 14971"], ["risk", "ISO 14971", "风险管理"]),
    ("clinical evaluation", ["临床", "clinical", "cer", "pmcf"], ["clinical", "CER", "PMCF", "临床"]),
    ("post-market surveillance", ["上市后", "pms", "post-market", "surveillance"], ["PMS", "post-market surveillance", "上市后"]),
    ("vigilance / incident reporting / recall", ["不良事件", "vigilance", "recall", "fsca"], ["vigilance", "recall", "FSCA", "不良事件"]),
    ("labeling / UDI / IFU", ["标签", "label", "ifu", "udi"], ["labeling", "UDI", "IFU", "标签"]),
    ("sterilization", ["灭菌", "sterilization", "bioburden"], ["sterilization", "bioburden", "灭菌"]),
    ("biological evaluation", ["生物相容性", "biocompatibility", "iso 10993"], ["biocompatibility", "ISO 10993", "生物相容性"]),
    ("packaging validation", ["包装", "packaging", "seal", "sterile barrier"], ["packaging", "seal", "sterile barrier", "包装"]),
    ("software / cybersecurity", ["软件", "software", "cybersecurity"], ["software", "cybersecurity", "软件"]),
    ("design control / change control", ["设计", "design", "engineering change", "change control"], ["design", "change control", "设计变更"]),
    ("document and record control", ["文件", "record", "documentation"], ["documentation", "record", "文件记录"]),
    ("supplier / purchasing control", ["供应商", "supplier", "purchasing"], ["supplier", "purchasing", "供应商"]),
    ("complaint handling", ["投诉", "complaint", "feedback"], ["complaint", "feedback", "投诉"]),
    ("training", ["培训", "training"], ["training", "培训"]),
    ("internal audit / external audit", ["审核", "audit"], ["audit", "审核"]),
]


@dataclass
class RegulationItem:
    row_index: int
    original_standard_code: str
    original_year: str
    latest_standard_code: str
    latest_year: str
    status: str
    update_summary: str
    reference_hint: str
    normalized_id: str
    standard_type: str


@dataclass
class EvidenceRecord:
    normalized_id: str
    raw_query: str
    source_title: str
    source_url: str
    source_domain: str
    source_quality: str
    evidence_type: str
    evidence_snippet: str = ""
    extracted_change_type: str = ""
    changed_area: str = ""
    confidence: str = "Low"
    notes: str = ""


class DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_result = False
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "a" and "result__a" in attrs_dict.get("class", ""):
            self._in_result = True
            self._href = attrs_dict.get("href", "")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._in_result:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_result:
            title = clean_whitespace("".join(self._text))
            url = clean_search_url(self._href)
            if title and url:
                self.results.append({"title": title, "url": url})
            self._in_result = False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build amendment profiles for updated regulations and standards.")
    parser.add_argument("--excel", default="AI-assisted regulatory updates.xlsx")
    parser.add_argument("--sheet", default=PREFERRED_SHEET)
    parser.add_argument("--output-dir", default="regulation_amendment_profiles")
    parser.add_argument("--regulation-files-dir", default="regulation_files")
    parser.add_argument("--only-updated", default="true")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--official-only", default="false")
    parser.add_argument("--resume", default="true")
    parser.add_argument("--web-search", default="false")
    return parser.parse_args(argv)


def run_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return collect_regulation_amendments(
        excel_path=Path(args.excel),
        sheet=args.sheet,
        output_dir=Path(args.output_dir),
        regulation_files_dir=Path(args.regulation_files_dir),
        only_updated=parse_bool(args.only_updated),
        max_items=args.max_items,
        official_only=parse_bool(args.official_only),
        resume=parse_bool(args.resume),
        web_search=parse_bool(args.web_search),
    )


def collect_regulation_amendments(
    excel_path: Path,
    sheet: str = PREFERRED_SHEET,
    output_dir: Path = Path("regulation_amendment_profiles"),
    regulation_files_dir: Path = Path("regulation_files"),
    only_updated: bool = True,
    max_items: int | None = None,
    official_only: bool = False,
    resume: bool = True,
    web_search: bool = False,
    use_file_comparison: bool = True,
    include_editorial_changes: bool = False,
) -> dict[str, Any]:
    items = load_excel_items(excel_path, sheet, only_updated)
    if max_items is not None:
        items = items[:max_items]
    profiles = build_amendment_profiles(
        items,
        output_dir=output_dir,
        regulation_files_dir=regulation_files_dir,
        official_only=official_only,
        resume=resume,
        web_search=web_search,
        use_file_comparison=use_file_comparison,
        include_editorial_changes=include_editorial_changes,
    )
    return summarize_profiles(profiles, output_dir)


def build_amendment_profiles(
    rows_or_items: pd.DataFrame | list[RegulationItem],
    output_dir: Path = Path("regulation_amendment_profiles"),
    regulation_files_dir: Path = Path("regulation_files"),
    official_only: bool = False,
    resume: bool = True,
    web_search: bool = False,
    use_file_comparison: bool = True,
    include_editorial_changes: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sources").mkdir(exist_ok=True)
    items = dataframe_to_items(rows_or_items) if isinstance(rows_or_items, pd.DataFrame) else rows_or_items
    profiles: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []

    total_items = len(items)
    if progress_callback:
        progress_callback(0, total_items, "Preparing amendment profiles")

    for completed, item in enumerate(items, start=1):
        item_dir = output_dir / "sources" / item.normalized_id
        profile_path = item_dir / f"amendment_profile_row_{item.row_index}.json"
        cache_key = profile_cache_key(item)
        try:
            if resume and profile_path.exists():
                cached = json.loads(profile_path.read_text(encoding="utf-8"))
                if cached.get("cache_key") == cache_key:
                    cached["profile_path"] = str(profile_path)
                    profiles.append(cached)
                    log_rows.append(profile_log_row(item, cached.get("source_mode", ""), "hit", "Reused cached amendment profile."))
                    if progress_callback:
                        progress_callback(completed, total_items, f"Reused amendment profile {completed}/{total_items}")
                    continue

            item_dir.mkdir(parents=True, exist_ok=True)
            profile = build_one_profile(
                item,
                regulation_files_dir,
                official_only=official_only,
                web_search=web_search,
                use_file_comparison=use_file_comparison,
                include_editorial_changes=include_editorial_changes,
                profile_output_dir=item_dir,
            )
            profile["cache_key"] = cache_key
            profile["profile_path"] = str(profile_path)
            profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
            profiles.append(profile)
            log_rows.append(profile_log_row(item, profile["source_mode"], "miss", "Created amendment profile."))
            if progress_callback:
                progress_callback(completed, total_items, f"Built amendment profile {completed}/{total_items}")
        except Exception as exc:
            error_rows.append(
                {
                    "row_index": item.row_index,
                    "original_standard_code": item.original_standard_code,
                    "latest_standard_code": item.latest_standard_code,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "timestamp": now_iso(),
                }
            )
            profile = minimal_profile(item, "no_amendment_evidence", "", "Unexpected profile-building error.")
            profile["cache_key"] = cache_key
            profile["profile_path"] = str(profile_path)
            item_dir.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
            profiles.append(profile)
            if progress_callback:
                progress_callback(completed, total_items, f"Recorded amendment profile error {completed}/{total_items}")

    write_profile_outputs(output_dir, profiles, log_rows, error_rows)
    return profiles


def build_one_profile(
    item: RegulationItem,
    regulation_files_dir: Path,
    official_only: bool = False,
    web_search: bool = False,
    use_file_comparison: bool = True,
    include_editorial_changes: bool = False,
    profile_output_dir: Path | None = None,
) -> dict[str, Any]:
    old_file, latest_file = locate_regulation_files(item, regulation_files_dir)
    if use_file_comparison and old_file and latest_file:
        return file_comparison_profile(item, old_file, latest_file, include_editorial_changes, profile_output_dir)

    evidence = collect_free_summary_evidence(item, official_only=official_only, web_search=web_search)
    usable = [row for row in evidence if row.evidence_type in {"official_change_evidence", "free_change_summary"}]
    if usable:
        best = usable[0]
        mode = "official_summary" if best.source_quality == "official" else "free_online_summary"
        return evidence_profile(item, best, evidence, old_file=old_file, latest_file=latest_file)

    if item.update_summary:
        confidence = "Medium" if is_specific_summary(item.update_summary) else "Low"
        return excel_fallback_profile(item, "excel_amendment_summary", item.update_summary, confidence)
    if item.reference_hint:
        return excel_fallback_profile(item, "excel_reference_hint", item.reference_hint, "Low")
    return minimal_profile(item, "no_amendment_evidence", "", "No usable file comparison, free summary, Excel amendment summary, or reference hint was available.")


def file_comparison_profile(
    item: RegulationItem,
    old_file: Path,
    latest_file: Path,
    include_editorial_changes: bool = False,
    profile_output_dir: Path | None = None,
) -> dict[str, Any]:
    comparison = compare_regulation_versions(
        old_file=old_file,
        latest_file=latest_file,
        regulation_id=item.normalized_id,
        old_version=item.original_year or item.original_standard_code,
        latest_version=item.latest_year or item.latest_standard_code,
        include_editorial_changes=include_editorial_changes,
    )
    if profile_output_dir:
        if comparison.get("comparison_validation_failed"):
            profile_output_dir.mkdir(parents=True, exist_ok=True)
            (profile_output_dir / "comparison_validation_failed.json").write_text(
                json.dumps(
                    {
                        "validation_failures": comparison.get("validation_failures", []),
                        "extraction_warnings": comparison.get("extraction_warnings", []),
                        "old_body_clause_index": comparison.get("old_body_clause_index", []),
                        "latest_body_clause_index": comparison.get("latest_body_clause_index", []),
                        "annex_a_mapping_reference": comparison.get("annex_a_mapping_reference", {}),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            write_comparison_audit(profile_output_dir, comparison)
    summary = summarize_comparison_profile(comparison)
    profile = base_profile(item)
    profile.update(
        {
            "source_mode": "old_vs_latest_file_comparison",
            "amendment_summary_used": summary,
            "full_text_available": True,
            "old_version_available": True,
            "latest_version_available": True,
            "exact_clause_verified": bool(comparison.get("exact_clause_verified")),
            "exact_amendment_known": bool(comparison.get("reviewable_change_count")),
            "confidence": "High" if comparison.get("clause_comparisons") else "Medium",
            "official_summary_available": bool(comparison.get("official_comparison", {}).get("detected")),
            "official_summary_source": comparison.get("official_comparison", {}).get("source", ""),
            "classification_counts": comparison.get("classification_counts", {}),
            "comparison_validation_failed": bool(comparison.get("comparison_validation_failed", False)),
            "validation_failures": comparison.get("validation_failures", []),
            "extraction_warnings": comparison.get("extraction_warnings", []),
            "clause_comparisons": comparison.get("clause_comparisons", []),
            "ai_review_change_classes": comparison.get("reviewable_change_classes", []),
            "include_editorial_changes": include_editorial_changes,
            "claimed_changes": [
                {
                    "topic": change.get("latest_clause_id") or change.get("old_clause_id", ""),
                    "claimed_change": f"{change.get('change_classification', '')}: {change.get('evidence_preview', '')[:300]}",
                    "source": "Local old/latest regulation file comparison",
                    "source_type": "old_vs_latest_file_comparison",
                    "confidence": change.get("impact_level", "Medium"),
                    "exact_clause_verified": True,
                }
                for change in comparison.get("clause_comparisons", [])
                if change.get("change_classification") in set(comparison.get("reviewable_change_classes", []))
            ],
            "affected_qms_topics": comparison.get("affected_qms_topics", []),
            "search_keywords": comparison.get("search_keywords", []),
            "limitations": comparison.get("limitations", []),
            "needs_human_confirmation": True,
        }
    )
    if profile_output_dir:
        (profile_output_dir / "amendment_profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile


def evidence_profile(
    item: RegulationItem,
    best: EvidenceRecord,
    evidence: list[EvidenceRecord],
    old_file: Path | None,
    latest_file: Path | None,
) -> dict[str, Any]:
    summary = best.evidence_snippet or best.source_title
    topics, keywords = extract_qms_topics_and_keywords(" ".join([summary, item.update_summary, item.reference_hint]))
    mode = "official_summary" if best.source_quality == "official" else "free_online_summary"
    profile = base_profile(item)
    profile.update(
        {
            "source_mode": mode,
            "amendment_summary_used": summary,
            "old_version_available": bool(old_file),
            "latest_version_available": bool(latest_file),
            "exact_clause_verified": mode == "official_summary" and "clause" in summary.casefold(),
            "exact_amendment_known": True,
            "confidence": "High" if mode == "official_summary" else "Medium",
            "claimed_changes": [
                {
                    "topic": best.changed_area or "amendment summary",
                    "claimed_change": summary,
                    "source": best.source_url,
                    "source_type": best.source_quality,
                    "confidence": best.confidence,
                    "exact_clause_verified": mode == "official_summary" and "clause" in summary.casefold(),
                }
            ],
            "affected_qms_topics": topics,
            "search_keywords": keywords,
            "official_source_urls": [row.source_url for row in evidence if row.source_quality == "official"],
            "limitations": [
                "Full old and latest regulation text was not available for local clause-by-clause comparison.",
                "Amendment profile is based on free online summary evidence.",
                "Human QA/RA confirmation is required before controlled document amendment.",
            ],
            "needs_human_confirmation": True,
        }
    )
    return profile


def excel_fallback_profile(item: RegulationItem, mode: str, text: str, confidence: str) -> dict[str, Any]:
    topics, keywords = extract_qms_topics_and_keywords(text)
    source = "Excel 主要变更内容" if mode == "excel_amendment_summary" else "Excel 查询参考"
    profile = base_profile(item)
    profile.update(
        {
            "source_mode": mode,
            "amendment_summary_used": text,
            "exact_amendment_known": True,
            "confidence": confidence,
            "claimed_changes": [
                {
                    "topic": topic or "Excel amendment summary",
                    "claimed_change": f"According to {source}, the update appears to affect: {text}",
                    "source": source,
                    "source_type": "excel_internal_summary" if mode == "excel_amendment_summary" else "excel_reference_hint",
                    "confidence": confidence,
                    "exact_clause_verified": False,
                }
                for topic in (topics or [""])
            ],
            "affected_qms_topics": topics,
            "search_keywords": keywords,
            "limitations": excel_limitations(source),
            "needs_human_confirmation": True,
        }
    )
    return profile


def minimal_profile(item: RegulationItem, mode: str, text: str, reason: str) -> dict[str, Any]:
    profile = base_profile(item)
    profile.update(
        {
            "source_mode": mode,
            "amendment_summary_used": text,
            "exact_amendment_known": False,
            "confidence": "Low",
            "claimed_changes": [],
            "limitations": [
                reason,
                "Full old and latest regulation text was not available.",
                "Exact clause-by-clause amendment could not be verified.",
                "Human QA/RA confirmation is required before controlled document amendment.",
            ],
            "needs_human_confirmation": True,
        }
    )
    return profile


def base_profile(item: RegulationItem) -> dict[str, Any]:
    return {
        "row_index": item.row_index,
        "regulation_id": item.normalized_id,
        "normalized_id": item.normalized_id,
        "original_standard_code": item.original_standard_code,
        "original_year": item.original_year,
        "latest_standard_code": item.latest_standard_code,
        "latest_year": item.latest_year,
        "status_from_excel": item.status,
        "source_mode": "no_amendment_evidence",
        "amendment_summary_used": "",
        "excel_amendment_summary": item.update_summary,
        "excel_reference_hint": item.reference_hint,
        "full_text_available": False,
        "old_version_available": False,
        "latest_version_available": False,
        "exact_clause_verified": False,
        "exact_amendment_known": True,
        "confidence": "Low",
        "claimed_changes": [],
        "affected_qms_topics": [],
        "search_keywords": [],
        "likely_affected_document_types": ["QM", "QSP", "WI", "FM"],
        "limitations": [],
        "needs_human_confirmation": True,
        "official_source_urls": [],
        "searched_queries": [],
        "prompt_version": PROMPT_VERSION,
    }


def load_excel_items(excel_path: Path, sheet: str, only_updated: bool = True) -> list[RegulationItem]:
    sheets = pd.ExcelFile(excel_path).sheet_names
    selected_sheet = sheet if sheet in sheets else sheets[0]
    df = pd.read_excel(excel_path, sheet_name=selected_sheet)
    df.columns = [str(column).strip() for column in df.columns]
    if only_updated:
        status = df.get("状态", pd.Series([""] * len(df))).astype(str)
        df = df[status.str.contains("已更新", na=False)].copy()
    return dataframe_to_items(df)


def dataframe_to_items(df: pd.DataFrame) -> list[RegulationItem]:
    items: list[RegulationItem] = []
    for index, row in df.reset_index().iterrows():
        original = clean_cell(row.get("原标准编号", ""))
        latest = clean_cell(row.get("最新版本", ""))
        normalized_id, standard_type = normalize_identity(original, latest)
        items.append(
            RegulationItem(
                row_index=int(row.get("index", index)) + 2,
                original_standard_code=original,
                original_year=clean_cell(row.get("原版本年份", "")),
                latest_standard_code=latest,
                latest_year=clean_cell(row.get("最新年份", "")),
                status=clean_cell(row.get("状态", "")),
                update_summary=clean_cell(row.get("主要变更内容", "")),
                reference_hint=clean_cell(row.get("查询参考", "")),
                normalized_id=normalized_id,
                standard_type=standard_type,
            )
        )
    return items


def locate_regulation_files(item: RegulationItem, regulation_files_dir: Path) -> tuple[Path | None, Path | None]:
    if not regulation_files_dir.exists() or not regulation_files_dir.is_dir():
        return None, None
    files = [path for path in regulation_files_dir.rglob("*") if path.is_file() and path.suffix.casefold() in {".txt", ".docx", ".xlsx", ".pdf"}]
    old = best_file_match(files, item, item.original_standard_code, item.original_year)
    latest = best_file_match(files, item, item.latest_standard_code, item.latest_year)
    return old, latest


def best_file_match(files: list[Path], item: RegulationItem, code: str, year: str) -> Path | None:
    tokens = file_tokens(code, item.normalized_id)
    year_text = str(year)
    best: tuple[int, Path] | None = None
    for path in files:
        name = path.name.casefold()
        score = sum(1 for token in tokens if token and token.casefold() in name)
        if year_text and year_text != "nan" and year_text in name:
            score += 3
        if score and (best is None or score > best[0]):
            best = (score, path)
    return best[1] if best else None


def file_tokens(code: str, normalized_id: str) -> list[str]:
    values = [code, normalized_id, normalized_id.replace("_", " "), normalized_id.replace("_", "-")]
    values.extend(re.findall(r"[A-Z]+[_\s-]?\d+(?:[_\s-]\d+)?", normalized_id.upper()))
    return [sanitize_file_token(value) for value in values if sanitize_file_token(value)]


def sanitize_file_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", " ", str(value)).strip().casefold()


def compare_clause_blocks(old_text: str, latest_text: str) -> list[dict[str, str]]:
    old_blocks = split_clause_blocks(old_text)
    latest_blocks = split_clause_blocks(latest_text)
    changes: list[dict[str, str]] = []
    for clause, latest in latest_blocks.items():
        old = old_blocks.get(clause)
        if old is None:
            changes.append({"clause": clause, "summary": f"Clause-like block {clause} appears to be new or newly extractable."})
        elif normalize_compare_text(old) != normalize_compare_text(latest):
            changes.append({"clause": clause, "summary": f"Clause-like block {clause} text differs between old and latest versions."})
    for clause in old_blocks:
        if clause not in latest_blocks:
            changes.append({"clause": clause, "summary": f"Clause-like block {clause} appears removed or renumbered."})
    return changes


def split_clause_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current = "general"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^(\d+(?:\.\d+){0,4})\s+(.+)", stripped)
        if match:
            current = match.group(1)
            blocks.setdefault(current, []).append(stripped)
        else:
            blocks.setdefault(current, []).append(stripped)
    return {key: "\n".join(value) for key, value in blocks.items()}


def summarize_detected_changes(changes: list[dict[str, str]]) -> str:
    if not changes:
        return "No clause-like differences were detected from the available extracted text."
    clauses = ", ".join(change["clause"] for change in changes[:10])
    suffix = "" if len(changes) <= 10 else f" and {len(changes) - 10} additional clause-like blocks"
    return f"Local comparison detected text differences in clause-like block(s): {clauses}{suffix}."


def summarize_comparison_profile(comparison: dict[str, Any]) -> str:
    counts = comparison.get("classification_counts", {})
    substantive = int(counts.get("substantive_or_uncertain", 0))
    unchanged = int(counts.get("unchanged", 0))
    editorial = int(counts.get("editorial_change_only", 0))
    mapped = int(counts.get("mapped_clauses", comparison.get("mapped_clause_count", 0)))
    official = "official comparison material detected" if comparison.get("official_comparison", {}).get("detected") else "no official comparison material detected"
    return (
        f"Old-vs-latest file comparison mapped {mapped} clause relationship(s): "
        f"{substantive} substantive/uncertain, {editorial} editorial-only, {unchanged} unchanged; {official}."
    )


def collect_free_summary_evidence(item: RegulationItem, official_only: bool = False, web_search: bool = False) -> list[EvidenceRecord]:
    evidence: list[EvidenceRecord] = []
    searched_queries = generate_queries(item)
    if not web_search:
        return evidence
    try:
        evidence.extend(search_official_catalog(item))
    except Exception:
        pass
    for query in searched_queries:
        # Network search is intentionally conservative; search result titles alone are not enough
        # unless they explicitly describe changes/amendments.
        for result in search_web_metadata(query):
            quality = classify_source_quality(result["url"], item.standard_type)
            if official_only and quality != "official":
                continue
            evidence.append(
                EvidenceRecord(
                    normalized_id=item.normalized_id,
                    raw_query=query,
                    source_title=result["title"],
                    source_url=result["url"],
                    source_domain=source_domain(result["url"]),
                    source_quality=quality,
                    evidence_type="free_change_summary" if title_has_change_summary(result["title"]) else "catalog_metadata",
                    evidence_snippet=result["title"],
                    confidence="High" if quality == "official" else "Medium",
                )
            )
    return evidence


def search_official_catalog(item: RegulationItem) -> list[EvidenceRecord]:
    if item.standard_type != "iso":
        return []
    rows: list[EvidenceRecord] = []
    for query in iso_catalog_queries(item):
        payload = {"requests": [{"indexName": "PROD_isoorg_en", "params": f"query={quote_plus(query)}&hitsPerPage=10"}]}
        response = requests.post(ISO_ALGOLIA_URL, headers=ISO_ALGOLIA_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        hits = response.json().get("results", [{}])[0].get("hits", [])
        for hit in hits:
            title = clean_whitespace(str(hit.get("title", "")))
            text = clean_whitespace(str(hit.get("text", "")))
            reference = clean_whitespace(str(hit.get("reference", "")))
            combined = " ".join([title, text, reference])
            if not related_to_item(reference, item) or not title_has_change_summary(combined):
                continue
            rows.append(
                EvidenceRecord(
                    normalized_id=item.normalized_id,
                    raw_query=f"official ISO catalog API: {query}",
                    source_title=title,
                    source_url="https://www.iso.org" + str(hit.get("seoURL", "")),
                    source_domain="iso.org",
                    source_quality="official",
                    evidence_type="official_change_evidence",
                    evidence_snippet=text[:900] or title,
                    extracted_change_type=extract_change_type(combined),
                    changed_area=extract_changed_area(combined),
                    confidence="High",
                )
            )
    return rows


def search_web_metadata(query: str) -> list[dict[str, str]]:
    response = requests.get(
        SEARCH_URL.format(query=quote_plus(query)),
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    parser = DuckDuckGoParser()
    parser.feed(response.text)
    return parser.results[:8]


def generate_queries(item: RegulationItem) -> list[str]:
    normalized_text = item.normalized_id.replace("_", " ")
    queries = [
        f'"{item.original_standard_code}" "{item.latest_standard_code}" amendment changes',
        f'"{item.latest_standard_code}" "what changed"',
        f'"{item.latest_standard_code}" amendment',
        f'"{item.latest_standard_code}" corrigendum',
        f'"{normalized_text}" revision changes',
        f'"{normalized_text}" transition provisions',
    ]
    for domain in official_domains_for_type(item.standard_type):
        queries.append(f'site:{domain} "{item.latest_standard_code}" amendment')
        queries.append(f'site:{domain} "{normalized_text}" changes')
    if item.reference_hint:
        queries.append(item.reference_hint)
    return dedupe_strings([query for query in queries if query and '""' not in query])


def extract_qms_topics_and_keywords(text: str) -> tuple[list[str], list[str]]:
    lower = str(text or "").casefold()
    topics: list[str] = []
    keywords: list[str] = []
    for topic, triggers, additions in TOPIC_RULES:
        if any(trigger.casefold() in lower for trigger in triggers):
            topics.append(topic)
            keywords.extend(additions)
    keywords.extend(re.findall(r"\b(?:ISO|IEC|ASTM|EN|GB/T|YY/T)\s*[\w/-]+", str(text or ""), flags=re.I))
    return dedupe_strings(topics), dedupe_strings(keywords)


def is_specific_summary(text: str) -> bool:
    stripped = str(text or "").strip()
    if len(stripped) < 20:
        return False
    specific_terms = ["scope", "clause", "annex", "requirement", "definition", "test", "risk", "clinical", "label", "udi", "灭菌", "风险", "临床", "标签", "要求", "条款"]
    return any(term in stripped.casefold() for term in specific_terms) or len(stripped) >= 60


def assess_confidence(official_found: bool, details_confirmed: bool, evidence: list[EvidenceRecord]) -> str:
    if official_found and details_confirmed:
        return "High"
    if official_found or any(row.source_quality in {"authorized", "secondary"} for row in evidence):
        return "Medium"
    return "Low"


def profile_cache_key(item: RegulationItem) -> str:
    payload = {
        "row_index": item.row_index,
        "original_standard_code": item.original_standard_code,
        "original_year": item.original_year,
        "latest_standard_code": item.latest_standard_code,
        "latest_year": item.latest_year,
        "status": item.status,
        "update_summary_hash": text_hash(item.update_summary),
        "reference_hint_hash": text_hash(item.reference_hint),
        "prompt_version": PROMPT_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def write_profile_outputs(output_dir: Path, profiles: list[dict[str, Any]], log_rows: list[dict[str, Any]], error_rows: list[dict[str, Any]]) -> None:
    master_rows = [profile_master_row(profile) for profile in profiles]
    write_csv(output_dir / "amendment_profiles_master.csv", MASTER_COLUMNS, master_rows)
    (output_dir / "amendment_profiles_master.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
    unresolved = [row for row in master_rows if row["source_mode"] == "no_amendment_evidence" and row["confidence"] == "Low"]
    write_csv(output_dir / "unresolved_amendments.csv", MASTER_COLUMNS, unresolved)
    write_csv(output_dir / "amendment_profile_log.csv", LOG_COLUMNS, log_rows)
    write_csv(output_dir / "errors.csv", ERROR_COLUMNS, error_rows)


def profile_master_row(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "row_index": profile.get("row_index", ""),
        "original_standard_code": profile.get("original_standard_code", ""),
        "original_year": profile.get("original_year", ""),
        "latest_standard_code": profile.get("latest_standard_code", ""),
        "latest_year": profile.get("latest_year", ""),
        "status": profile.get("status_from_excel", ""),
        "source_mode": profile.get("source_mode", ""),
        "confidence": profile.get("confidence", ""),
        "exact_clause_verified": profile.get("exact_clause_verified", False),
        "exact_amendment_known": profile.get("exact_amendment_known", False),
        "full_text_available": profile.get("full_text_available", False),
        "old_version_available": profile.get("old_version_available", False),
        "latest_version_available": profile.get("latest_version_available", False),
        "amendment_summary_used": profile.get("amendment_summary_used", ""),
        "affected_qms_topics": " | ".join(profile.get("affected_qms_topics", [])),
        "search_keywords": " | ".join(profile.get("search_keywords", [])),
        "needs_human_confirmation": profile.get("needs_human_confirmation", True),
        "limitations": " | ".join(profile.get("limitations", [])),
        "profile_path": profile.get("profile_path", ""),
    }


def summarize_profiles(profiles: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for profile in profiles:
        mode = str(profile.get("source_mode", ""))
        counts[mode] = counts.get(mode, 0) + 1
    return {
        "total_updated_rows_processed": len(profiles),
        "profiles_created": len(profiles),
        "old_vs_latest_file_comparison": counts.get("old_vs_latest_file_comparison", 0),
        "file_comparison": counts.get("file_comparison", 0),
        "free_online_summary": counts.get("free_online_summary", 0),
        "official_summary": counts.get("official_summary", 0),
        "excel_amendment_summary": counts.get("excel_amendment_summary", 0),
        "excel_reference_hint": counts.get("excel_reference_hint", 0),
        "no_amendment_evidence": counts.get("no_amendment_evidence", 0),
        "requires_human_confirmation": sum(bool(profile.get("needs_human_confirmation")) for profile in profiles),
        "high_confidence": sum(profile.get("confidence") == "High" for profile in profiles),
        "medium_confidence": sum(profile.get("confidence") == "Medium" for profile in profiles),
        "low_confidence": sum(profile.get("confidence") == "Low" for profile in profiles),
        "output_folder_path": str(output_dir.resolve()),
    }


def profile_log_row(item: RegulationItem, source_mode: str, cache_status: str, message: str) -> dict[str, Any]:
    return {
        "timestamp": now_iso(),
        "row_index": item.row_index,
        "regulation_id": item.normalized_id,
        "source_mode": source_mode,
        "cache_status": cache_status,
        "message": message,
    }


def normalize_identity(original: str, latest: str = "") -> tuple[str, str]:
    text = f"{original} {latest}".upper()
    if re.search(r"2017/745|REGULATION\s*\(?EU\)?\s*2017/745|EU\s*MDR", text):
        return "EU_2017_745", "eu"
    if re.search(r"93/42/EEC|MDD", text):
        return "EU_93_42_EEC", "eu"
    astm = re.search(r"\bASTM\s*([A-Z])\s*-?\s*(\d+)", text)
    if astm:
        return f"ASTM_{astm.group(1)}{astm.group(2)}", "astm"
    iso = re.search(r"\b(?:BS\s+EN\s+|EN\s+)?ISO\s*(\d+(?:-\d+)?)", text)
    if iso:
        return f"ISO_{iso.group(1).replace('-', '_')}", "iso"
    iec = re.search(r"\bIEC\s*(\d+(?:-\d+)?)", text)
    if iec:
        return f"IEC_{iec.group(1).replace('-', '_')}", "iec"
    yy = re.search(r"\bYY/T\s*(\d+)", text)
    if yy:
        return f"YY_T_{yy.group(1)}", "china"
    gb = re.search(r"\bGB/T\s*(\d+)", text)
    if gb:
        return f"GB_T_{gb.group(1)}", "china"
    cfr = re.search(r"\b21\s*CFR\s*(\d+)", text)
    if cfr:
        return f"21_CFR_{cfr.group(1)}", "fda"
    digest = hashlib.sha1(f"{original}|{latest}".encode("utf-8")).hexdigest()[:8]
    return f"REG_{digest}", "unknown"


def iso_catalog_queries(item: RegulationItem) -> list[str]:
    queries = []
    for value in [item.original_standard_code, item.latest_standard_code, item.normalized_id.replace("_", "-").replace("ISO-", "")]:
        match = re.search(r"\bISO\s*(\d+(?:[-‑]\d+)?)", str(value), re.I)
        if match:
            queries.extend([match.group(1).replace("‑", "-"), f"ISO {match.group(1).replace('‑', '-')}"])
    return dedupe_strings(queries)


def related_to_item(reference: str, item: RegulationItem) -> bool:
    reference_id, _ = normalize_identity(reference, "")
    return reference_id == item.normalized_id or (item.normalized_id in {"ISO_594_1", "ISO_594_2"} and reference_id == "ISO_80369_7")


def official_domains_for_type(standard_type: str) -> list[str]:
    domains = {
        "iso": ["iso.org"],
        "astm": ["astm.org"],
        "eu": ["eur-lex.europa.eu", "health.ec.europa.eu", "ec.europa.eu"],
        "fda": ["fda.gov", "ecfr.gov"],
        "china": ["openstd.samr.gov.cn", "samr.gov.cn", "nmpa.gov.cn"],
        "hk": ["mdd.gov.hk"],
        "iec": ["iec.ch", "webstore.iec.ch"],
    }
    return domains.get(standard_type, ["iso.org", "astm.org", "eur-lex.europa.eu", "openstd.samr.gov.cn", "fda.gov", "mdd.gov.hk"])


def classify_source_quality(url_or_domain: str, standard_type: str) -> str:
    domain = source_domain(url_or_domain) if "://" in url_or_domain else url_or_domain.lower()
    if any(domain == official or domain.endswith("." + official) for official in official_domains_for_type(standard_type)):
        return "official"
    if any(domain.endswith(allowed) for allowed in ["standards.iteh.ai", "intertek.com", "ul.com"]):
        return "secondary"
    return "secondary"


def title_has_change_summary(text: str) -> bool:
    lower = str(text or "").casefold()
    return any(term in lower for term in ["amendment", "corrigendum", "what changed", "changes to", "revision", "revised", "transition"])


def extract_change_type(text: str) -> str:
    terms = [term for term in ["amendment", "corrigendum", "revision", "revised", "transition"] if term in str(text).casefold()]
    return "; ".join(dedupe_strings(terms))


def extract_changed_area(text: str) -> str:
    terms = [term for term in ["scope", "annex", "clause", "definition", "requirement", "test method", "labeling", "transition"] if term in str(text).casefold()]
    return "; ".join(dedupe_strings(terms))


def source_domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def clean_search_url(url: str) -> str:
    if "duckduckgo.com/l/" in url:
        parsed = urlparse("https:" + url if url.startswith("//") else url)
        return unquote(parse_qs(parsed.query).get("uddg", [""])[0])
    return url


def excel_limitations(source: str) -> list[str]:
    return [
        "Full old and latest regulation text was not available.",
        "Exact clause-by-clause amendment could not be verified.",
        f"Amendment profile is based on {source}.",
        "Human QA/RA confirmation is required before controlled document amendment.",
    ]


def normalize_compare_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def clean_whitespace(value: str) -> str:
    return " ".join(str(value or "").split())


def clean_cell(value: object) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.casefold() == "nan" else text


def parse_bool(value: object) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def print_summary(summary: dict[str, Any]) -> None:
    print("Regulation amendment profile summary")
    for key, value in summary.items():
        print(f"- {key}: {value}")
