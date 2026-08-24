from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .app_paths import data_root
from .regulation_suggestions import generate_document_suggestion


DEFAULT_JOB_DIR = data_root() / "regulatory_update_jobs"
RUNNING_THREADS: dict[str, threading.Thread] = {}
THREAD_LOCK = threading.Lock()


def create_suggestion_job(
    analysis: dict[str, Any],
    affected_nodes: list[dict[str, Any]],
    job_dir: str | Path = DEFAULT_JOB_DIR,
) -> str:
    root = Path(job_dir)
    root.mkdir(parents=True, exist_ok=True)
    candidates = []
    for position, node in enumerate(affected_nodes):
        row = dict(node)
        row["candidate_position"] = position
        row["row_id"] = candidate_row_id(row, position)
        candidates.append(json_ready(row))
    identity = analysis.get("resolved_pair", {})
    payload = {
        "analysis_id": analysis.get("analysis_id", ""),
        "schema_version": analysis.get("schema_version", ""),
        "family_key": identity.get("family_key", ""),
        "old_year": identity.get("old_year", ""),
        "latest_year": identity.get("latest_year", ""),
        "old_file_hash": identity.get("old_file_hash", ""),
        "latest_file_hash": identity.get("latest_file_hash", ""),
        "accepted_changes": analysis.get("comparison", {}).get("accepted_changes", []),
        "candidates": candidates,
    }
    job_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    path = job_path(job_id, root)
    if path.exists():
        return job_id
    path.mkdir(parents=True, exist_ok=True)
    write_json(path / "analysis.json", analysis)
    write_json(path / "candidates.json", candidates)
    write_json(path / "results.json", [])
    write_json(path / "decisions.json", {})
    write_json(
        path / "meta.json",
        {
            "job_id": job_id,
            "analysis_id": analysis.get("analysis_id", ""),
            "family_key": identity.get("family_key", ""),
            "status": "created",
            "started_at": "",
            "updated_at": now_iso(),
            "completed_at": "",
            "total_candidates": len(candidates),
            "completed_count": 0,
            "suggestion_count": 0,
            "current_item": "",
            "errors": [],
            "completed_ids": [],
            "stop_requested": False,
        },
    )
    return job_id


def start_suggestion_job(
    job_id: str,
    documents: list[dict[str, Any]],
    ai_call: Callable[..., dict[str, Any]] | None = None,
    job_dir: str | Path = DEFAULT_JOB_DIR,
) -> bool:
    with THREAD_LOCK:
        existing = RUNNING_THREADS.get(job_id)
        if existing and existing.is_alive():
            return False
        thread = threading.Thread(
            target=run_suggestion_job,
            kwargs={"job_id": job_id, "documents": documents, "ai_call": ai_call, "job_dir": job_dir},
            daemon=True,
        )
        RUNNING_THREADS[job_id] = thread
        thread.start()
    return True


def run_suggestion_job(
    job_id: str,
    documents: list[dict[str, Any]],
    ai_call: Callable[..., dict[str, Any]] | None = None,
    job_dir: str | Path = DEFAULT_JOB_DIR,
    max_candidates: int | None = None,
) -> None:
    root = Path(job_dir)
    path = job_path(job_id, root)
    candidates = read_json(path / "candidates.json", [])
    analysis = read_json(path / "analysis.json", {})
    accepted_changes = analysis.get("comparison", {}).get("accepted_changes", [])
    meta = load_suggestion_job(job_id, root)
    completed_ids = set(meta.get("completed_ids", []))
    documents_by_path: dict[str, dict[str, Any]] = {}
    for document in documents:
        for value in [document.get("file_path"), document.get("source_file_path")]:
            if value:
                documents_by_path[str(value)] = document
    processed_this_run = 0

    meta.update({"status": "running", "started_at": meta.get("started_at") or now_iso(), "stop_requested": False})
    save_meta(path, meta)
    for candidate in candidates:
        meta = load_suggestion_job(job_id, root)
        if meta.get("stop_requested"):
            meta["status"] = "stopped"
            meta["current_item"] = ""
            save_meta(path, meta)
            return
        row_id = str(candidate.get("row_id", ""))
        if row_id in completed_ids:
            continue
        detail = f"{candidate.get('document_code', '')} | depth {candidate.get('depth', 0)}"
        meta["current_item"] = detail
        save_meta(path, meta)
        result = None
        error_entry = None
        try:
            file_path = str(candidate.get("file_path") or candidate.get("source_file_path") or "")
            document = documents_by_path.get(file_path)
            if not document:
                raise ValueError(f"Missing processed document for {file_path}")
            kwargs = {}
            if ai_call is not None:
                kwargs["ai_call"] = ai_call
            result = generate_document_suggestion(candidate, document, accepted_changes, **kwargs)
            result["row_id"] = row_id
            result["reviewed_at"] = now_iso()
            append_result(path, result)
        except Exception as exc:  # pragma: no cover - state is asserted by runner tests.
            error_entry = {"row_id": row_id, "candidate": detail, "error": str(exc), "at": now_iso()}

        meta = load_suggestion_job(job_id, root) or meta
        if result and result.get("suggestions"):
            meta["suggestion_count"] = int(meta.get("suggestion_count", 0)) + len(result["suggestions"])
        if error_entry:
            errors = [item for item in meta.get("errors", []) if item.get("row_id") != row_id]
            errors.append(error_entry)
            meta["errors"] = errors
        else:
            completed_ids.add(row_id)
            meta["errors"] = [item for item in meta.get("errors", []) if item.get("row_id") != row_id]
        meta["completed_ids"] = sorted(completed_ids)
        meta["completed_count"] = len(completed_ids)
        save_meta(path, meta)
        processed_this_run += 1
        if max_candidates is not None and processed_this_run >= max_candidates:
            meta["status"] = "stopped"
            meta["current_item"] = ""
            save_meta(path, meta)
            return

    meta = load_suggestion_job(job_id, root) or meta
    meta["status"] = "completed" if len(completed_ids) == len(candidates) else "stopped"
    meta["current_item"] = ""
    meta["completed_at"] = now_iso() if meta["status"] == "completed" else ""
    save_meta(path, meta)


def request_stop_suggestion_job(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> None:
    root = Path(job_dir)
    meta = load_suggestion_job(job_id, root)
    if not meta:
        return
    meta["stop_requested"] = True
    meta["status"] = "stopping"
    save_meta(job_path(job_id, root), meta)


def retry_suggestion_errors(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> None:
    root = Path(job_dir)
    meta = load_suggestion_job(job_id, root)
    if not meta:
        return
    meta["errors"] = []
    meta["status"] = "stopped"
    meta["stop_requested"] = False
    save_meta(job_path(job_id, root), meta)


def list_suggestion_jobs(job_dir: str | Path = DEFAULT_JOB_DIR) -> list[dict[str, Any]]:
    root = Path(job_dir)
    if not root.exists():
        return []
    jobs = [read_json(path / "meta.json", {}) for path in root.iterdir() if path.is_dir()]
    return sorted([item for item in jobs if item], key=lambda item: str(item.get("updated_at", "")), reverse=True)


def load_suggestion_job(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> dict[str, Any]:
    return read_json(job_path(job_id, Path(job_dir)) / "meta.json", {})


def load_suggestion_analysis(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> dict[str, Any]:
    return read_json(job_path(job_id, Path(job_dir)) / "analysis.json", {})


def load_suggestion_results(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> pd.DataFrame:
    return pd.DataFrame(read_json(job_path(job_id, Path(job_dir)) / "results.json", []))


def is_suggestion_job_running(job_id: str) -> bool:
    thread = RUNNING_THREADS.get(job_id)
    return bool(thread and thread.is_alive())


def save_suggestion_decision(
    job_id: str,
    row_id: str,
    decision: str,
    comment: str,
    job_dir: str | Path = DEFAULT_JOB_DIR,
) -> None:
    path = job_path(job_id, Path(job_dir)) / "decisions.json"
    decisions = read_json(path, {})
    decisions[row_id] = {"decision": decision, "comment": comment, "updated_at": now_iso()}
    write_json(path, decisions)


def load_suggestion_decisions(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> dict[str, Any]:
    return read_json(job_path(job_id, Path(job_dir)) / "decisions.json", {})


def candidate_row_id(candidate: dict[str, Any], position: int) -> str:
    payload = {
        "position": position,
        "document_code": candidate.get("document_code", ""),
        "file_path": candidate.get("file_path", ""),
        "depth": candidate.get("depth", 0),
        "relationship_path": candidate.get("relationship_path", ""),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def append_result(path: Path, result: dict[str, Any]) -> None:
    rows = read_json(path / "results.json", [])
    rows = [row for row in rows if row.get("row_id") != result.get("row_id")]
    rows.append(json_ready(result))
    rows.sort(key=lambda item: (int(item.get("depth", 0)), str(item.get("document_code", ""))))
    write_json(path / "results.json", rows)


def job_path(job_id: str, job_dir: Path) -> Path:
    return job_dir / job_id


def save_meta(path: Path, meta: dict[str, Any]) -> None:
    meta["updated_at"] = now_iso()
    write_json(path / "meta.json", meta)


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(json_ready(value), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    if value is pd.NA:
        return ""
    if hasattr(value, "item"):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if not isinstance(value, (str, bool, int, float, type(None))):
        return str(value)
    return value


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
