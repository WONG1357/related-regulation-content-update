from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from modules.ai_amendment_agent import ai_is_configured
from modules.document_library import load_document_library
from modules.export_utils import audit_workbook_bytes
from modules.reference_graph import REFERENCE_EXTRACTOR_VERSION
from modules.regulation_dependency import discover_affected_files
from modules.regulation_identity import RegulationIdentityError, resolve_regulation_pair
from modules.regulation_version_comparator import compare_regulation_versions
from modules.report_generator import generate_two_version_report
from modules.suggestion_runner import (
    create_suggestion_job,
    is_suggestion_job_running,
    list_suggestion_jobs,
    load_suggestion_decisions,
    load_suggestion_job,
    load_suggestion_results,
    request_stop_suggestion_job,
    retry_suggestion_errors,
    save_suggestion_decision,
    start_suggestion_job,
)


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DEFAULT_EXCEL = APP_DIR.parent / "AI-assisted regulatory updates.xlsx"
DEFAULT_DOC_DIR = APP_DIR.parent / "original file"
UPLOAD_ROOT = DATA_DIR / "two_version_uploads"
ANALYSIS_ROOT = DATA_DIR / "two_version_analyses"
SUGGESTION_JOB_DIR = DATA_DIR / "regulatory_update_jobs"
OUTPUT_ROOT = DATA_DIR / "two_version_outputs"
ALLOWED_UPLOAD_TYPES = ["pdf", "docx", "xlsx", "txt"]


st.set_page_config(
    page_title="Regulatory Impact Review",
    page_icon="↗",
    layout="wide",
    initial_sidebar_state="collapsed",
)
for env_path in (APP_DIR / ".env", APP_DIR / ".env.txt"):
    if env_path.exists():
        load_dotenv(env_path)


def init_state() -> None:
    defaults = {
        "two_version_analysis": None,
        "controlled_documents": [],
        "document_library_summary": {},
        "active_suggestion_job_id": "",
        "analysis_error": "",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(json_ready(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def safe_upload_name(value: str, fallback: str) -> str:
    name = Path(value or fallback).name
    cleaned = re.sub(r"[^\w.()\-\s]+", "_", name, flags=re.UNICODE).strip()
    return cleaned[:180] or fallback


def save_uploaded_pair(first_upload: Any, second_upload: Any) -> tuple[Path, Path, str]:
    first_bytes = first_upload.getvalue()
    second_bytes = second_upload.getvalue()
    pair_id = hashlib.sha256(first_bytes + b"\0" + second_bytes).hexdigest()[:16]
    directory = UPLOAD_ROOT / pair_id
    directory.mkdir(parents=True, exist_ok=True)
    first_name = safe_upload_name(first_upload.name, "version_a")
    second_name = safe_upload_name(second_upload.name, "version_b")
    first_path = directory / f"a_{first_name}"
    second_path = directory / f"b_{second_name}"
    first_path.write_bytes(first_bytes)
    second_path.write_bytes(second_bytes)
    return first_path, second_path, pair_id


def progress_callback(progress: Any, detail: Any) -> Callable[[int, int, str], None]:
    started = time.monotonic()

    def update(done: int, total: int, label: str) -> None:
        total = max(int(total), 1)
        ratio = min(max(float(done) / total, 0.0), 1.0)
        progress.progress(ratio, text=f"Controlled-document index · {ratio:.0%}")
        detail.caption(f"{label} · elapsed {time.monotonic() - started:.0f}s")

    return update


def run_two_version_analysis(first_upload: Any, second_upload: Any) -> dict[str, Any]:
    first_path, second_path, upload_pair_id = save_uploaded_pair(first_upload, second_upload)
    if not DEFAULT_EXCEL.is_file():
        raise FileNotFoundError("The configured alias workbook was not found.")
    if not DEFAULT_DOC_DIR.is_dir():
        raise FileNotFoundError("The configured controlled-document folder was not found.")

    status = st.status("Resolving the uploaded editions…", expanded=True)
    status.write("Reading document identity and workbook aliases")
    resolved_model = resolve_regulation_pair(first_path, second_path, DEFAULT_EXCEL)
    resolved = resolved_model.to_dict()
    resolved.update(
        {
            "family_key": resolved_model.family,
            "old_file_name": resolved_model.old.file_name,
            "latest_file_name": resolved_model.latest.file_name,
            "old_file_hash": file_sha256(Path(resolved_model.old_file)),
            "latest_file_hash": file_sha256(Path(resolved_model.latest_file)),
            "workbook_row_ids": list(resolved_model.matched_row_numbers),
        }
    )

    status.write("Comparing paired normative clauses and validating official material")
    comparison = compare_regulation_versions(
        Path(resolved_model.old_file),
        Path(resolved_model.latest_file),
        resolved_model.family,
        str(resolved_model.old_year),
        str(resolved_model.latest_year),
        include_editorial_changes=False,
    )

    status.write("Refreshing changed controlled files and Section 3 references")
    progress = st.progress(0, text="Controlled-document index · starting")
    detail = st.empty()
    library = load_document_library(
        DEFAULT_DOC_DIR,
        DATA_DIR / "converted_documents",
        cache_path=DATA_DIR / "document_library_cache.json",
        reference_audit_dir=DATA_DIR / "reference_audit",
        progress_callback=progress_callback(progress, detail),
    )
    documents = library["documents"]
    aliases = list(dict.fromkeys([*resolved_model.aliases, resolved_model.family]))
    dependency = discover_affected_files(documents, aliases).to_dict()
    progress.empty()
    detail.empty()

    analysis_id = hashlib.sha256(
        "|".join(
            [
                resolved["family_key"],
                str(resolved["old_year"]),
                str(resolved["latest_year"]),
                resolved["old_file_hash"],
                resolved["latest_file_hash"],
                REFERENCE_EXTRACTOR_VERSION,
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    analysis = {
        "schema_version": "two-version-regulatory-impact-v1",
        "analysis_id": analysis_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "upload_pair_id": upload_pair_id,
        "resolved_pair": resolved,
        "comparison": comparison,
        "dependency": dependency,
        "document_library": {
            "source_folder": "Configured controlled-document folder",
            "extractor_version": REFERENCE_EXTRACTOR_VERSION,
            "document_count": len(documents),
            "cache_stats": library.get("cache_stats", {}),
            "conversion_failures": library.get("conversion", {}).get("failed_files", []),
            "extraction_failures": library.get("extraction_failures", []),
        },
    }
    atomic_write_json(ANALYSIS_ROOT / analysis_id / "analysis.json", public_value(analysis))
    st.session_state.two_version_analysis = analysis
    st.session_state.controlled_documents = documents
    st.session_state.document_library_summary = analysis["document_library"]
    st.session_state.active_suggestion_job_id = ""
    status.update(label="Deterministic analysis complete", state="complete", expanded=False)
    return analysis


def text_value(value: Any) -> str:
    if isinstance(value, list):
        return " · ".join(str(item) for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value or "")


PATH_FIELD_NAMES = {
    "file_path",
    "source_file_path",
    "old_file",
    "latest_file",
    "workbook_path",
    "source_folder",
}


def public_path(value: Any) -> str:
    """Return a share-safe filename rather than a local path."""
    raw = str(value or "").strip()
    return Path(raw).name if raw else ""


def public_value(value: Any, field_name: str = "") -> Any:
    if field_name in PATH_FIELD_NAMES:
        return public_path(value)
    if isinstance(value, dict):
        return {key: public_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [public_value(item, field_name) for item in value]
    if isinstance(value, str):
        return value.replace(str(Path.home()), "<local-home>")
    return value


def public_error_message(error: Exception) -> str:
    return str(error).replace(str(Path.home()), "<local-home>")


def public_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([public_value(dict(row)) for row in rows])


def accepted_change_frame(analysis: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for change in analysis.get("comparison", {}).get("accepted_changes", []):
        rows.append(
            {
                "Evidence ID": change.get("evidence_id", ""),
                "Earlier clause": change.get("old_clause_id", ""),
                "Later clause": change.get("latest_clause_id", ""),
                "Classification": change.get("change_classification", ""),
                "Evidence method": change.get("evidence_method", ""),
                "Official statement": change.get("official_change_summary", ""),
                "Changed requirement units": text_value(
                    change.get("changed_requirement_units") or change.get("added_requirement_units", [])
                ),
                "Earlier text": change.get("old_clause_text", ""),
                "Later text": change.get("latest_clause_text", ""),
                "Human confirmation": bool(change.get("needs_human_confirmation", False)),
            }
        )
    return pd.DataFrame(rows)


def affected_file_frame(analysis: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for node in analysis.get("dependency", {}).get("nodes", []):
        rows.append(
            {
                "Depth": node.get("depth", 0),
                "Document code": node.get("document_code", ""),
                "Title": node.get("document_title", ""),
                "Direct seed": bool(node.get("direct_match", False)),
                "Seed": node.get("seed_code", ""),
                "Relationship path": " → ".join(node.get("relationship_path", [])),
                "Matched aliases": text_value(node.get("matched_aliases", [])),
                "Section 3 evidence": text_value(node.get("section3_evidence", [])),
                "File": public_path(node.get("file_path", "")),
            }
        )
    return pd.DataFrame(rows)


def unresolved_frame(analysis: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for item in analysis.get("dependency", {}).get("unresolved_references", []):
        row = public_value(dict(item))
        if isinstance(row.get("relationship_path"), list):
            row["relationship_path"] = " → ".join(row["relationship_path"])
        rows.append(row)
    return pd.DataFrame(rows)


def flatten_suggestion_results(results: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for result in results.to_dict("records") if not results.empty else []:
        suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []
        base = {
            "row_id": result.get("row_id", ""),
            "document_code": result.get("document_code", ""),
            "document_title": result.get("document_title", ""),
            "file_path": public_path(result.get("file_path", "")),
            "depth": result.get("depth", 0),
            "dependency_path": text_value(result.get("relationship_path", "")),
            "decision": result.get("decision", ""),
            "confidence": result.get("confidence", ""),
            "reason": result.get("reason", ""),
            "limitations": text_value(result.get("limitations", [])),
            "qa_ra_confirmation": result.get("qa_ra_review_status", "Draft only - not approved"),
        }
        if not suggestions:
            rows.append(base)
            continue
        for item in suggestions:
            rows.append(
                {
                    **base,
                    "target_section": item.get("section_id", ""),
                    "location": item.get("location", ""),
                    "current_text": item.get("current_text", ""),
                    "proposed_text": item.get("proposed_text", ""),
                    "rationale": item.get("rationale", ""),
                    "regulation_evidence_ids": text_value(item.get("evidence_ids", [])),
                }
            )
    return pd.DataFrame(rows)


def input_resolution_frame(analysis: dict[str, Any]) -> pd.DataFrame:
    resolved = analysis.get("resolved_pair", {})
    return pd.DataFrame(
        [
            {
                "analysis_id": analysis.get("analysis_id", ""),
                "family": resolved.get("family", ""),
                "earlier_year": resolved.get("old_year", ""),
                "later_year": resolved.get("latest_year", ""),
                "match_mode": resolved.get("match_mode", ""),
                "earlier_file": resolved.get("old", {}).get("file_name", ""),
                "later_file": resolved.get("latest", {}).get("file_name", ""),
                "earlier_sha256": resolved.get("old_file_hash", ""),
                "later_sha256": resolved.get("latest_file_hash", ""),
                "aliases": text_value(resolved.get("aliases", [])),
                "workbook_rows": text_value(resolved.get("matched_row_numbers", [])),
                "workbook": "Configured alias registry",
                "controlled_document_folder": "Configured controlled-document folder",
                "comparison_method": analysis.get("comparison", {}).get("selected_method", ""),
            }
        ]
    )


def audit_sheets(analysis: dict[str, Any], suggestion_rows: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "Input Resolution": input_resolution_frame(analysis),
        "Paired Changes": public_frame(analysis.get("comparison", {}).get("accepted_changes", [])),
        "Dependency Paths": public_frame(analysis.get("dependency", {}).get("nodes", [])),
        "Unresolved References": unresolved_frame(analysis),
        "Suggestions": suggestion_rows,
    }


def ensure_documents_loaded() -> list[dict[str, Any]]:
    documents = st.session_state.controlled_documents
    if documents:
        return documents
    library = load_document_library(
        DEFAULT_DOC_DIR,
        DATA_DIR / "converted_documents",
        cache_path=DATA_DIR / "document_library_cache.json",
        reference_audit_dir=DATA_DIR / "reference_audit",
    )
    st.session_state.controlled_documents = library["documents"]
    return library["documents"]


def render_metric_cards(values: list[tuple[str, Any, str]]) -> None:
    columns = st.columns(len(values))
    for column, (label, value, help_text) in zip(columns, values):
        with column:
            st.metric(label, value, help=help_text)


def render_upload_stage(analysis: dict[str, Any] | None) -> None:
    st.subheader("Upload two editions")
    st.caption(
        "Upload exactly two versions of the same regulation. Edition order is inferred from the documents and filenames."
    )
    source_left, source_right = st.columns(2)
    with source_left:
        st.markdown("**Alias registry · read only**")
        st.code("Configured alias registry: AI-assisted regulatory updates.xlsx", language=None)
    with source_right:
        st.markdown("**Controlled-document source · read only**")
        st.code("Configured controlled-document folder: original file", language=None)

    first_col, second_col = st.columns(2)
    with first_col:
        first_upload = st.file_uploader(
            "Regulation file A",
            type=ALLOWED_UPLOAD_TYPES,
            key="regulation_file_a",
            help="PDF, DOCX, XLSX, or TXT",
        )
    with second_col:
        second_upload = st.file_uploader(
            "Regulation file B",
            type=ALLOWED_UPLOAD_TYPES,
            key="regulation_file_b",
            help="PDF, DOCX, XLSX, or TXT",
        )

    ready = first_upload is not None and second_upload is not None
    if (first_upload is None) != (second_upload is None):
        st.info("Add the second edition to continue.", icon="↗")
    if st.button(
        "Resolve identity and analyze impact",
        type="primary",
        disabled=not ready,
        use_container_width=True,
    ):
        st.session_state.analysis_error = ""
        try:
            analysis = run_two_version_analysis(first_upload, second_upload)
            st.rerun()
        except RegulationIdentityError as exc:
            st.session_state.analysis_error = f"{exc} [{exc.code}]"
        except Exception as exc:
            st.session_state.analysis_error = public_error_message(exc)

    if st.session_state.analysis_error:
        st.error(st.session_state.analysis_error, icon="⚠")

    if not analysis:
        return
    resolved = analysis["resolved_pair"]
    st.divider()
    st.subheader("Resolved regulation identity")
    render_metric_cards(
        [
            ("Family", resolved.get("family", "—"), "Normalized regulation family"),
            ("Earlier edition", resolved.get("old_year", "—"), resolved.get("old", {}).get("file_name", "")),
            ("Later edition", resolved.get("latest_year", "—"), resolved.get("latest", {}).get("file_name", "")),
            ("Registry match", resolved.get("match_mode", "—"), "Exact edition or accepted family fallback"),
        ]
    )
    for warning in resolved.get("warnings", []):
        st.warning(warning, icon="⚠")
    with st.expander(f"Resolved aliases ({len(resolved.get('aliases', []))})"):
        st.write(resolved.get("aliases", []))
        st.dataframe(pd.DataFrame(resolved.get("matched_rows", [])), use_container_width=True, hide_index=True)


def render_change_stage(analysis: dict[str, Any] | None) -> None:
    st.subheader("Accepted change evidence")
    if not analysis:
        st.info("Complete Upload & Resolve first.")
        return
    comparison = analysis["comparison"]
    accepted = comparison.get("accepted_changes", [])
    all_rows = comparison.get("clause_comparisons", [])
    conflicts = [row for row in all_rows if row.get("evidence_conflict")]
    excluded = max(len(all_rows) - len(accepted), 0)
    render_metric_cards(
        [
            ("Method", comparison.get("selected_method", "—").replace("_", " "), "Selected evidence route"),
            ("Accepted changes", len(accepted), "Eligible for content-based drafting"),
            ("Excluded rows", excluded, "Editorial, unchanged, unmatched, unresolved, or renumber-only"),
            ("Conflicts", len(conflicts), "Official/direct disagreement requiring human review"),
        ]
    )
    st.caption(f"Evidence source: {comparison.get('evidence_source', '—')}")
    official = comparison.get("official_comparison", {})
    if official.get("detected") and not official.get("usable_as_primary"):
        st.warning(
            "Official comparison material was detected but rejected as primary evidence: "
            + str(official.get("rejection_reason") or "uploaded editions were not explicitly matched or rows did not validate."),
            icon="⚠",
        )
    if comparison.get("comparison_validation_failed"):
        st.error("Comparison validation failed closed; no row can be used for drafting.")
    for warning in comparison.get("extraction_warnings", []):
        st.warning(str(warning), icon="⚠")
    frame = accepted_change_frame(analysis)
    if frame.empty:
        st.info("No paired substantive change met the drafting evidence contract.")
    else:
        st.dataframe(
            frame,
            use_container_width=True,
            hide_index=True,
            height=min(620, 88 + len(frame) * 36),
            column_config={
                "Earlier text": st.column_config.TextColumn(width="large"),
                "Later text": st.column_config.TextColumn(width="large"),
                "Official statement": st.column_config.TextColumn(width="large"),
            },
        )
    if conflicts:
        with st.expander(f"Rows requiring human confirmation ({len(conflicts)})"):
            st.dataframe(public_frame(conflicts), use_container_width=True, hide_index=True)


def render_affected_stage(analysis: dict[str, Any] | None) -> None:
    st.subheader("Recursive affected-file set")
    if not analysis:
        st.info("Complete Upload & Resolve first.")
        return
    dependency = analysis["dependency"]
    nodes = dependency.get("nodes", [])
    unresolved = dependency.get("unresolved_references", [])
    max_depth = max((int(node.get("depth", 0)) for node in nodes), default=0)
    render_metric_cards(
        [
            ("Direct Section 3 seeds", len(dependency.get("direct_seed_codes", [])), "Alias matches in Section 3 only"),
            ("Affected files", len(nodes), "Deduplicated traversal result"),
            ("Maximum depth", max_depth, "Minimum forward dependency depth"),
            ("Unresolved codes", len(dependency.get("unresolved_codes", [])), "Audit only; excluded from AI drafting"),
        ]
    )
    frame = affected_file_frame(analysis)
    if frame.empty:
        st.info("No controlled document cited the resolved alias family in Section 3.")
    else:
        st.dataframe(frame, use_container_width=True, hide_index=True, height=min(650, 88 + len(frame) * 35))
    if unresolved:
        st.warning(
            f"{len(unresolved)} unresolved Section 3 reference record(s) remain visible for audit and will not be drafted.",
            icon="⚠",
        )
        with st.expander("Unresolved reference audit"):
            st.dataframe(unresolved_frame(analysis), use_container_width=True, hide_index=True)
    library = analysis.get("document_library", {})
    failures = [*library.get("conversion_failures", []), *library.get("extraction_failures", [])]
    with st.expander("Index coverage and cache audit"):
        st.json(
            public_value({
                "document_count": library.get("document_count", 0),
                "cache_stats": library.get("cache_stats", {}),
                "extractor_version": library.get("extractor_version", ""),
                "failures": failures,
            }),
            expanded=False,
        )


def render_job_controls(analysis: dict[str, Any], job_id: str) -> tuple[dict[str, Any], pd.DataFrame]:
    meta = load_suggestion_job(job_id, SUGGESTION_JOB_DIR)
    results = load_suggestion_results(job_id, SUGGESTION_JOB_DIR)
    running = is_suggestion_job_running(job_id)
    total = int(meta.get("total_candidates", 0))
    completed = int(meta.get("completed_count", 0))
    st.progress(completed / max(total, 1), text=f"{completed} of {total} controlled files reviewed")
    st.caption(
        f"Status: {meta.get('status', 'unknown')}"
        + (f" · Current: {meta.get('current_item')}" if meta.get("current_item") else "")
    )
    refresh_col, pause_col, resume_col, retry_col = st.columns(4)
    with refresh_col:
        if st.button("Refresh", use_container_width=True):
            st.rerun()
    with pause_col:
        if st.button("Pause", disabled=not running, use_container_width=True):
            request_stop_suggestion_job(job_id, SUGGESTION_JOB_DIR)
            st.rerun()
    with resume_col:
        if st.button(
            "Resume",
            disabled=running or meta.get("status") == "completed",
            use_container_width=True,
        ):
            start_suggestion_job(job_id, ensure_documents_loaded(), job_dir=SUGGESTION_JOB_DIR)
            st.rerun()
    with retry_col:
        if st.button("Retry errors", disabled=running or not meta.get("errors"), use_container_width=True):
            retry_suggestion_errors(job_id, SUGGESTION_JOB_DIR)
            start_suggestion_job(job_id, ensure_documents_loaded(), job_dir=SUGGESTION_JOB_DIR)
            st.rerun()
    if meta.get("errors"):
        st.error(f"{len(meta['errors'])} item(s) failed. Review the error audit or retry.")
        with st.expander("Suggestion errors"):
            st.dataframe(pd.DataFrame(meta["errors"]), use_container_width=True, hide_index=True)
    return meta, results


def render_reviewer_confirmation(job_id: str, results: pd.DataFrame) -> None:
    if results.empty:
        return
    records = results.to_dict("records")
    label_by_id = {
        str(item.get("row_id", "")): f"{item.get('document_code', 'Document')} · {item.get('decision', '')}"
        for item in records
    }
    st.markdown("#### QA/RA confirmation")
    selected = st.selectbox(
        "Suggestion result",
        options=list(label_by_id),
        format_func=lambda value: label_by_id[value],
    )
    existing = load_suggestion_decisions(job_id, SUGGESTION_JOB_DIR).get(selected, {})
    with st.form("qa_ra_decision_form"):
        decision_options = ["Pending", "Accept for document-control review", "Reject", "Need follow-up"]
        current = existing.get("decision", "Pending")
        decision = st.selectbox(
            "Reviewer decision",
            decision_options,
            index=decision_options.index(current) if current in decision_options else 0,
        )
        comment = st.text_area("Reviewer comment", value=existing.get("comment", ""))
        if st.form_submit_button("Save QA/RA decision", type="primary"):
            save_suggestion_decision(job_id, selected, decision, comment, SUGGESTION_JOB_DIR)
            st.success("Reviewer decision saved.")


def render_exports(analysis: dict[str, Any], job_id: str, results: pd.DataFrame) -> None:
    suggestion_rows = flatten_suggestion_results(results)
    workbook = audit_workbook_bytes(audit_sheets(analysis, suggestion_rows))
    decisions = load_suggestion_decisions(job_id, SUGGESTION_JOB_DIR) if job_id else {}
    result_records = results.to_dict("records") if not results.empty else []
    output_dir = OUTPUT_ROOT / analysis["analysis_id"]
    report_path = output_dir / "regulatory_impact_amendment_report.docx"
    generate_two_version_report(report_path, analysis, result_records, decisions)
    left, right = st.columns(2)
    with left:
        st.download_button(
            "Download audit workbook",
            data=workbook,
            file_name=f"regulatory-impact-audit-{analysis['analysis_id']}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    with right:
        st.download_button(
            "Download Word amendment report",
            data=report_path.read_bytes(),
            file_name=f"regulatory-impact-report-{analysis['analysis_id']}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            use_container_width=True,
        )


def render_suggestion_stage(analysis: dict[str, Any] | None) -> None:
    st.subheader("Content-based amendment suggestions")
    if not analysis:
        st.info("Complete Upload & Resolve first.")
        return
    accepted = analysis.get("comparison", {}).get("accepted_changes", [])
    nodes = analysis.get("dependency", {}).get("nodes", [])
    if not accepted:
        st.warning("No accepted paired change evidence is available; AI drafting is disabled.")
    elif not nodes:
        st.info("No affected controlled files were found; there is nothing to draft.")
    elif not ai_is_configured():
        st.error(
            "AI drafting is unavailable because AI_API_KEY is not configured. "
            "Identity resolution, change evidence, dependency traversal, and exports remain available."
        )
    can_generate = bool(accepted and nodes and ai_is_configured())
    job_id = st.session_state.active_suggestion_job_id
    if not job_id:
        saved_jobs = [
            item
            for item in list_suggestion_jobs(SUGGESTION_JOB_DIR)
            if item.get("analysis_id") == analysis.get("analysis_id")
        ]
        if saved_jobs:
            saved_ids = [str(item.get("job_id", "")) for item in saved_jobs if item.get("job_id")]
            job_id = st.selectbox(
                "Saved suggestion run",
                saved_ids,
                format_func=lambda value: f"{value} · {next((item.get('status') for item in saved_jobs if item.get('job_id') == value), '')}",
            )
            st.session_state.active_suggestion_job_id = job_id
    if not job_id and st.button(
        "Generate amendment suggestions",
        type="primary",
        disabled=not can_generate,
        use_container_width=True,
    ):
        job_id = create_suggestion_job(analysis, nodes, SUGGESTION_JOB_DIR)
        st.session_state.active_suggestion_job_id = job_id
        start_suggestion_job(job_id, ensure_documents_loaded(), job_dir=SUGGESTION_JOB_DIR)
        st.rerun()

    results = pd.DataFrame()
    if job_id:
        _, results = render_job_controls(analysis, job_id)
        suggestion_rows = flatten_suggestion_results(results)
        if suggestion_rows.empty:
            st.info("No result rows have been persisted yet. Use Refresh to check progress.")
        else:
            st.dataframe(suggestion_rows, use_container_width=True, hide_index=True, height=min(650, 88 + len(suggestion_rows) * 36))
        render_reviewer_confirmation(job_id, results)

    st.divider()
    st.markdown("#### Audit exports")
    st.caption("Exports are evidence records only. Controlled documents are never modified by this app.")
    render_exports(analysis, job_id, results)


def render_header() -> None:
    st.markdown(
        """
        <section class="app-hero">
          <div class="eyebrow">QMS · REGULATORY INTELLIGENCE</div>
          <h1>Two-Version Regulatory Impact Review</h1>
          <p>Resolve one regulation family, validate substantive clause changes, trace Section 3 dependencies, and draft review-ready amendments.</p>
        </section>
        """,
        unsafe_allow_html=True,
    )


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        :root { color-scheme: light; }
        .stApp { background: #f6f8fa; color: #17202a; }
        [data-testid="stHeader"] { background: rgba(246,248,250,.92); }
        .block-container { max-width: 1440px; padding-top: 2rem; padding-bottom: 4rem; }
        .app-hero {
          background: linear-gradient(122deg, #0f2f36 0%, #164e55 62%, #176b68 100%);
          border: 1px solid rgba(255,255,255,.14); border-radius: 20px; color: white;
          padding: 2.2rem 2.5rem; margin-bottom: 1.35rem; box-shadow: 0 14px 40px rgba(15,47,54,.14);
        }
        .app-hero h1 { color: white; font-size: clamp(2rem, 4vw, 3.25rem); line-height: 1.05; margin: .45rem 0 .75rem; letter-spacing: -.035em; }
        .app-hero p { color: #d7eeee; font-size: 1.04rem; max-width: 850px; margin: 0; line-height: 1.55; }
        .eyebrow { color: #9fe3db; font-size: .75rem; font-weight: 750; letter-spacing: .14em; }
        [data-testid="stMetric"] { background: white; border: 1px solid #d9e1e5; border-radius: 14px; padding: 1rem 1.1rem; box-shadow: 0 4px 15px rgba(31,45,55,.045); }
        [data-testid="stMetricLabel"] { color: #52616b; }
        [data-testid="stMetricValue"] { color: #123f43; font-weight: 720; }
        .stTabs [data-baseweb="tab-list"] { gap: .4rem; background: #eaf0f2; padding: .35rem; border-radius: 12px; }
        .stTabs [data-baseweb="tab"] { min-height: 2.85rem; border-radius: 9px; padding: .65rem 1.1rem; color: #36464f; }
        .stTabs [aria-selected="true"] { background: white !important; color: #0f5f5c !important; box-shadow: 0 1px 6px rgba(20,50,55,.10); }
        .stButton > button[kind="primary"], .stDownloadButton > button { border-radius: 10px; min-height: 2.8rem; font-weight: 700; }
        .stButton > button[kind="primary"] { background: #0f766e; border-color: #0f766e; }
        .stButton > button[kind="primary"]:hover { background: #115e59; border-color: #115e59; }
        button:focus-visible, input:focus-visible, textarea:focus-visible, [role="tab"]:focus-visible { outline: 3px solid #f4b942 !important; outline-offset: 2px !important; }
        [data-testid="stFileUploader"] { background: white; border: 1px solid #d8e1e5; border-radius: 14px; padding: .4rem .8rem; }
        [data-testid="stAlert"] { border-radius: 12px; }
        code { white-space: pre-wrap !important; overflow-wrap: anywhere; }
        @media (max-width: 800px) {
          .block-container { padding: 1rem .75rem 3rem; }
          .app-hero { padding: 1.5rem 1.25rem; border-radius: 16px; }
          .stTabs [data-baseweb="tab-list"] { overflow-x: auto; }
          .stTabs [data-baseweb="tab"] { white-space: nowrap; padding: .55rem .75rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


init_state()
inject_styles()
render_header()
analysis = st.session_state.two_version_analysis
upload_tab, evidence_tab, affected_tab, suggestions_tab = st.tabs(
    ["1 · Upload & Resolve", "2 · Change Evidence", "3 · Affected Files", "4 · Suggestions & Export"]
)
with upload_tab:
    render_upload_stage(analysis)
with evidence_tab:
    render_change_stage(analysis)
with affected_tab:
    render_affected_stage(analysis)
with suggestions_tab:
    render_suggestion_stage(analysis)
