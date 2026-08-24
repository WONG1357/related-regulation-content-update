from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from .app_paths import data_root
from .document_converter import convert_documents
from .document_extractor import extract_document, refresh_document_references
from .reference_graph import REFERENCE_EXTRACTOR_VERSION, attach_reference_matches, export_reference_audit


DEFAULT_CACHE_PATH = data_root() / "document_library_cache.json"
DEFAULT_REFERENCE_AUDIT_DIR = data_root() / "reference_audit"

DocumentExtractor = Callable[[str | Path], dict[str, Any]]
ProgressCallback = Callable[[int, int, str], None]


def load_document_library(
    original_dir: str | Path,
    converted_dir: str | Path,
    cache_path: str | Path = DEFAULT_CACHE_PATH,
    reference_audit_dir: str | Path = DEFAULT_REFERENCE_AUDIT_DIR,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    conversion = convert_documents(original_dir, converted_dir)
    items: list[dict[str, str]] = []
    for item in conversion.get("ready_files", []):
        file_path = str(item.get("file_path", ""))
        items.append({"source_path": file_path, "extracted_path": file_path})
    for item in conversion.get("converted_files", []):
        extracted_path = str(item.get("file_path", ""))
        items.append(
            {
                "source_path": str(item.get("source_file") or extracted_path),
                "extracted_path": extracted_path,
            }
        )
    documents, extraction_failures, cache_stats = load_or_extract_documents(
        items,
        cache_path=cache_path,
        extractor_version=REFERENCE_EXTRACTOR_VERSION,
        original_file_dir=original_dir,
        progress_callback=progress_callback,
    )
    graph = attach_reference_matches(documents)
    export_reference_audit(documents, graph, reference_audit_dir)
    return {
        "documents": documents,
        "extraction_failures": extraction_failures,
        "conversion": conversion,
        "cache_stats": cache_stats,
        "reference_graph": graph,
    }


def load_or_extract_documents(
    items: list[dict[str, str]],
    cache_path: str | Path,
    extractor_version: str,
    extractor: DocumentExtractor = extract_document,
    refresh_references: bool = True,
    original_file_dir: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, int]]:
    path = Path(cache_path)
    cached_payload = read_json(path, {})
    cached_records = {
        str(record.get("source_path", "")): record
        for record in cached_payload.get("records", [])
        if isinstance(record, dict)
    }
    records: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    hits = 0
    misses = 0
    ordered_items = sorted(items, key=lambda item: str(item.get("source_path", "")))
    total = len(ordered_items)

    for position, item in enumerate(ordered_items, start=1):
        source_path = Path(str(item.get("source_path", "")))
        extracted_path = Path(str(item.get("extracted_path", "")))
        if progress_callback:
            progress_callback(position - 1, max(total, 1), source_path.name)
        try:
            content_hash = file_hash(source_path)
            cached = cached_records.get(str(source_path), {})
            if (
                cached.get("source_hash") == content_hash
                and cached.get("extractor_version") == extractor_version
                and isinstance(cached.get("document"), dict)
            ):
                document = dict(cached["document"])
                hits += 1
            else:
                document = dict(extractor(extracted_path))
                if refresh_references:
                    refresh_document_references(document, source_path, original_file_dir or source_path.parent)
                misses += 1
            document["source_file_path"] = str(source_path)
            document["source_file_name"] = source_path.name
            records.append(
                {
                    "source_path": str(source_path),
                    "extracted_path": str(extracted_path),
                    "source_hash": content_hash,
                    "extractor_version": extractor_version,
                    "document": document,
                }
            )
            documents.append(document)
        except Exception as exc:
            failures.append({"file_path": str(source_path or extracted_path), "reason": str(exc)})
    write_json(
        path,
        {
            "extractor_version": extractor_version,
            "updated_at": time.time(),
            "records": records,
        },
    )
    if progress_callback:
        progress_callback(total, max(total, 1), "Controlled-document index ready")
    return documents, failures, {"hits": hits, "misses": misses, "total": total}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
