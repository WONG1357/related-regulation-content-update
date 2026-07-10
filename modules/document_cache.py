from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .reference_graph import REFERENCE_EXTRACTOR_VERSION


DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[1] / "data" / "document_analysis_cache.json"
DEFAULT_SUMMARY_DIR = Path(__file__).resolve().parents[1] / "data" / "document_summaries"
PROFILE_FIELDS = [
    "main_sections",
    "reference_regulations",
    "reference_standards",
    "reference_guidance",
    "reference_documents",
    "reference_forms",
    "reference_items",
    "document_relationships",
    "external_references",
    "matched_reference_files",
    "unresolved_internal_references",
]


def normalized_file_name(file_name: str) -> str:
    return re.sub(r"\s+", " ", str(file_name or "").strip()).casefold()


def document_content_hash(document: dict[str, Any]) -> str:
    chunks = []
    for section in document.get("sections", []):
        chunks.append(str(section.get("section_id", "")))
        chunks.append(str(section.get("page_or_location", "")))
        chunks.append(str(section.get("text", "")))
    return hashlib.sha256("\n".join(chunks).encode("utf-8")).hexdigest()


def document_text(document: dict[str, Any], max_chars: int | None = None) -> str:
    parts = []
    for section in document.get("sections", []):
        section_id = section.get("section_id", "")
        location = section.get("page_or_location", "")
        text = str(section.get("text", "")).strip()
        if text:
            parts.append(f"[{section_id} | {location}] {text}")
    result = "\n".join(parts)
    if max_chars is not None and len(result) > max_chars:
        return result[:max_chars] + "\n[TRUNCATED]"
    return result


def load_cache(cache_path: str | Path = DEFAULT_CACHE_PATH) -> list[dict[str, Any]]:
    path = Path(cache_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def save_cache(records: list[dict[str, Any]], cache_path: str | Path = DEFAULT_CACHE_PATH) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    if path == DEFAULT_CACHE_PATH:
        export_document_summaries(records)


def cached_profile_for_document(
    document: dict[str, Any],
    cache_path: str | Path = DEFAULT_CACHE_PATH,
) -> dict[str, Any] | None:
    file_name = str(document.get("file_name", ""))
    normalized_name = normalized_file_name(file_name)
    content_hash = document_content_hash(document)
    for record in load_cache(cache_path):
        if (
            record.get("normalized_file_name") == normalized_name
            and record.get("content_hash") == content_hash
            and record.get("reference_extractor_version") == REFERENCE_EXTRACTOR_VERSION
            and all(field in record for field in PROFILE_FIELDS)
        ):
            return record
    return None


def attach_cached_profiles(
    documents: list[dict[str, Any]],
    cache_path: str | Path = DEFAULT_CACHE_PATH,
) -> int:
    attached = 0
    for document in documents:
        profile = cached_profile_for_document(document, cache_path)
        if not profile:
            continue
        document["cached_content_summary"] = profile.get("content_summary", "")
        document["cached_key_sections"] = profile.get("key_sections", [])
        attached += 1
    return attached


def export_document_summaries(
    records: list[dict[str, Any]] | None = None,
    output_dir: str | Path = DEFAULT_SUMMARY_DIR,
) -> dict[str, str]:
    records = load_cache() if records is None else records
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    lines = ["# Document Summaries", ""]
    for record in sorted(records, key=lambda item: str(item.get("document_code", "")) + str(item.get("file_name", ""))):
        row = {
            "document_code": record.get("document_code", ""),
            "document_title": record.get("document_title", ""),
            "document_type": record.get("document_type", ""),
            "file_name": record.get("file_name", ""),
            "file_path": record.get("file_path", ""),
            "content_hash": record.get("content_hash", ""),
            "analyzed_at": record.get("analyzed_at", ""),
            "content_summary": record.get("content_summary", ""),
            "key_sections": record.get("key_sections", []),
        }
        summary_rows.append(row)
        title = " | ".join(str(row[key]) for key in ["document_code", "document_title", "file_name"] if row.get(key))
        lines.append(f"## {title or 'Untitled document'}")
        lines.append("")
        lines.append(f"- File: {row['file_name']}")
        lines.append(f"- Analyzed at: {row['analyzed_at']}")
        lines.append("")
        lines.append(str(row["content_summary"] or "").strip() or "(No summary)")
        lines.append("")
        key_sections = row.get("key_sections") if isinstance(row.get("key_sections"), list) else []
        if key_sections:
            lines.append("Key sections:")
            for section in key_sections:
                if not isinstance(section, dict):
                    continue
                section_id = str(section.get("section_id", ""))
                location = str(section.get("location", ""))
                summary = str(section.get("summary", ""))
                lines.append(f"- {section_id} {location}: {summary}".strip())
            lines.append("")
    json_path = output / "document_summaries.json"
    md_path = output / "document_summaries.md"
    json_path.write_text(json.dumps(summary_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def get_or_create_profile(
    document: dict[str, Any],
    analyze_document: Callable[[dict[str, Any]], dict[str, Any]],
    cache_path: str | Path = DEFAULT_CACHE_PATH,
) -> dict[str, Any]:
    records = load_cache(cache_path)
    file_name = str(document.get("file_name", ""))
    normalized_name = normalized_file_name(file_name)
    content_hash = document_content_hash(document)

    cached = cached_profile_for_document(document, cache_path)
    if cached:
        return cached

    analysis = analyze_document(document)
    record = {
        "file_name": file_name,
        "normalized_file_name": normalized_name,
        "file_path": document.get("file_path", ""),
        "content_hash": content_hash,
        "document_code": document.get("document_code", ""),
        "document_title": document.get("document_title", ""),
        "document_type": document.get("document_type", ""),
        "reference_extractor_version": REFERENCE_EXTRACTOR_VERSION,
        "content_summary": analysis.get("content_summary", ""),
        "key_sections": analysis.get("key_sections", []),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }
    for field in PROFILE_FIELDS:
        record[field] = document.get(field, [])

    records = [item for item in records if item.get("normalized_file_name") != normalized_name]
    records.append(record)
    save_cache(records, cache_path)
    return record
