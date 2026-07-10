from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .document_cache import DEFAULT_CACHE_PATH
from .impact_analyzer import DocumentAnalyzer, PairReviewer, review_one_candidate


DEFAULT_JOB_DIR = Path(__file__).resolve().parents[1] / "data" / "ai_review_jobs"
RUNNING_THREADS: dict[str, threading.Thread] = {}
THREAD_LOCK = threading.Lock()


def create_review_job(
    candidate_df: pd.DataFrame,
    documents: list[dict[str, Any]] | None = None,
    amendment_profiles: list[dict[str, Any]] | None = None,
    job_dir: str | Path = DEFAULT_JOB_DIR,
) -> str:
    del documents, amendment_profiles
    root = Path(job_dir)
    root.mkdir(parents=True, exist_ok=True)
    review_candidates = candidate_df[candidate_df["review_required"] == True].copy() if not candidate_df.empty else pd.DataFrame()  # noqa: E712
    candidate_rows = []
    for position, (_, row) in enumerate(review_candidates.reset_index(drop=True).iterrows()):
        item = row.to_dict()
        item["row_id"] = candidate_row_id(item, position)
        item["candidate_position"] = position
        candidate_rows.append(json_ready(item))
    job_id = hashlib.sha256(
        json.dumps(candidate_rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    path = job_path(job_id, root)
    if path.exists():
        return job_id
    path.mkdir(parents=True, exist_ok=True)
    write_json(path / "candidates.json", candidate_rows)
    write_json(path / "results.json", [])
    write_json(path / "decisions.json", {})
    write_json(
        path / "meta.json",
        {
            "job_id": job_id,
            "status": "created",
            "started_at": "",
            "updated_at": now_iso(),
            "completed_at": "",
            "total_candidates": len(candidate_rows),
            "completed_count": 0,
            "affected_count": 0,
            "current_item": "",
            "errors": [],
            "completed_ids": [],
            "stop_requested": False,
        },
    )
    return job_id


def start_review_job(
    job_id: str,
    documents: list[dict[str, Any]],
    cache_path: str | Path = DEFAULT_CACHE_PATH,
    document_analyzer: DocumentAnalyzer | None = None,
    pair_reviewer: PairReviewer | None = None,
    job_dir: str | Path = DEFAULT_JOB_DIR,
) -> bool:
    with THREAD_LOCK:
        existing = RUNNING_THREADS.get(job_id)
        if existing and existing.is_alive():
            return False
        thread = threading.Thread(
            target=run_review_job,
            kwargs={
                "job_id": job_id,
                "documents": documents,
                "cache_path": cache_path,
                "document_analyzer": document_analyzer,
                "pair_reviewer": pair_reviewer,
                "job_dir": job_dir,
            },
            daemon=True,
        )
        RUNNING_THREADS[job_id] = thread
        thread.start()
    return True


def run_review_job(
    job_id: str,
    documents: list[dict[str, Any]],
    cache_path: str | Path = DEFAULT_CACHE_PATH,
    document_analyzer: DocumentAnalyzer | None = None,
    pair_reviewer: PairReviewer | None = None,
    job_dir: str | Path = DEFAULT_JOB_DIR,
    max_candidates: int | None = None,
) -> None:
    root = Path(job_dir)
    path = job_path(job_id, root)
    candidates = read_json(path / "candidates.json", [])
    meta = load_review_job(job_id, root)
    completed_ids = set(meta.get("completed_ids", []))
    documents_by_path = {str(document.get("file_path", "")): document for document in documents}
    profile_cache: dict[str, dict[str, Any]] = {}
    processed_this_run = 0

    meta.update({"status": "running", "started_at": meta.get("started_at") or now_iso(), "stop_requested": False})
    save_meta(path, meta)
    for candidate in candidates:
        meta = load_review_job(job_id, root)
        if meta.get("stop_requested"):
            meta["status"] = "stopped"
            meta["current_item"] = ""
            save_meta(path, meta)
            return
        row_id = str(candidate.get("row_id", ""))
        if row_id in completed_ids:
            continue
        detail = f"{candidate.get('document_code', '')} | {candidate.get('regulation_old', '')}"
        meta["current_item"] = detail
        save_meta(path, meta)
        result = None
        error_entry = None
        try:
            document = documents_by_path.get(str(candidate.get("affected_file_path", "")))
            if not document:
                raise ValueError(f"Missing processed document for {candidate.get('affected_file_path', '')}")
            result = review_one_candidate(
                pd.Series(candidate),
                document,
                cache_path=cache_path,
                document_analyzer=document_analyzer,
                pair_reviewer=pair_reviewer,
                profile_cache=profile_cache,
            )
            if result:
                result["row_id"] = row_id
                result["reviewed_at"] = now_iso()
                append_result(path, result)
        except Exception as exc:  # pragma: no cover - exercised through state assertions in tests.
            error_entry = {"row_id": row_id, "candidate": detail, "error": str(exc), "at": now_iso()}
        meta = load_review_job(job_id, root) or meta
        if result:
            meta["affected_count"] = int(meta.get("affected_count", 0)) + 1
        if error_entry:
            errors = list(meta.get("errors", []))
            errors.append(error_entry)
            meta["errors"] = errors
        else:
            completed_ids.add(row_id)
        meta["completed_ids"] = sorted(completed_ids)
        meta["completed_count"] = len(completed_ids)
        meta["updated_at"] = now_iso()
        save_meta(path, meta)
        processed_this_run += 1
        if max_candidates is not None and processed_this_run >= max_candidates:
            meta["status"] = "stopped"
            meta["current_item"] = ""
            save_meta(path, meta)
            return

    if len(completed_ids) < len(candidates):
        meta["status"] = "stopped"
    else:
        meta["status"] = "completed"
    meta["current_item"] = ""
    meta["completed_at"] = now_iso()
    meta["updated_at"] = now_iso()
    save_meta(path, meta)


def request_stop_review_job(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> None:
    root = Path(job_dir)
    meta = load_review_job(job_id, root)
    if not meta:
        return
    meta["stop_requested"] = True
    meta["status"] = "stopping"
    meta["updated_at"] = now_iso()
    save_meta(job_path(job_id, root), meta)


def list_review_jobs(job_dir: str | Path = DEFAULT_JOB_DIR) -> list[dict[str, Any]]:
    root = Path(job_dir)
    if not root.exists():
        return []
    jobs = []
    for path in root.iterdir():
        if not path.is_dir():
            continue
        meta = read_json(path / "meta.json", {})
        if meta:
            jobs.append(meta)
    return sorted(jobs, key=lambda item: str(item.get("updated_at", "")), reverse=True)


def is_job_running(job_id: str) -> bool:
    thread = RUNNING_THREADS.get(job_id)
    return bool(thread and thread.is_alive())


def load_review_job(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> dict[str, Any]:
    return read_json(job_path(job_id, Path(job_dir)) / "meta.json", {})


def load_review_results(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> pd.DataFrame:
    rows = read_json(job_path(job_id, Path(job_dir)) / "results.json", [])
    return pd.DataFrame(rows)


def save_reviewer_decision(
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


def load_reviewer_decisions(job_id: str, job_dir: str | Path = DEFAULT_JOB_DIR) -> dict[str, Any]:
    return read_json(job_path(job_id, Path(job_dir)) / "decisions.json", {})


def candidate_row_id(candidate: dict[str, Any], position: int) -> str:
    payload = {
        "position": position,
        "reg_index": candidate.get("reg_index", ""),
        "regulation_old": candidate.get("regulation_old", ""),
        "affected_file_path": candidate.get("affected_file_path", ""),
        "candidate_level": candidate.get("candidate_level", ""),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def append_result(path: Path, result: dict[str, Any]) -> None:
    rows = read_json(path / "results.json", [])
    rows = [row for row in rows if row.get("row_id") != result.get("row_id")]
    rows.append(json_ready(result))
    write_json(path / "results.json", rows)


def save_meta(path: Path, meta: dict[str, Any]) -> None:
    meta["updated_at"] = now_iso()
    write_json(path / "meta.json", json_ready(meta))


def job_path(job_id: str, job_dir: Path) -> Path:
    return job_dir / job_id


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
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if value is pd.NA:
        return ""
    if not isinstance(value, (str, bool, int, float, type(None))):
        try:
            if bool(pd.isna(value)):
                return ""
        except (TypeError, ValueError):
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    return value


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
