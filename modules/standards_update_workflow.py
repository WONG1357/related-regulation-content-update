"""Bounded standards-update workflow; catalogue facts and document evidence stay separate.

This module deliberately does not invoke the legacy QMS amendment engine. A textual
comparison is review evidence, not automatic approval to alter a controlled document.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .standards_catalogue import CatalogueSnapshot, discover_iso_updates
from .standards_diff import compare_standard_files
from .standards_query import QuerySpec


def run_update_workflow(snapshot: CatalogueSnapshot, query: QuerySpec) -> dict[str, Any]:
    """Find publications, preserve lineage, and prepare explicit comparison tasks."""
    result = discover_iso_updates(
        snapshot, query.start_date, query.end_date,
        standards=query.standards, ics_prefixes=query.ics_prefixes,
        committees=query.committees, keywords=query.keywords,
    )
    for update in result["updates"]:
        predecessors = update.get("predecessors", [])
        if update.get("supplement_type"):
            action = "Review the supplement together with its base edition; do not compare it as a complete new edition."
        elif not predecessors:
            action = "Resolve the predecessor, or review as a first edition; no old text is inferred."
        elif len(predecessors) > 1:
            action = "Review every predecessor and supplement in this replacement set before consolidating a baseline."
        else:
            action = "Provide both identified editions for local comparison."
        update["next_action"] = action
        update["content_comparison"] = {"status": "not_run", "reason": "Full texts have not been supplied."}

    created_at = datetime.now(timezone.utc).isoformat()
    identity = json.dumps({"query": query.to_dict(), "source_hash": snapshot.sha256}, sort_keys=True)
    result.update({
        "workflow_version": "1.0",
        "run_id": hashlib.sha256(identity.encode()).hexdigest()[:12] + "-" + uuid4().hex[:12],
        "query_fingerprint": hashlib.sha256(identity.encode()).hexdigest(),
        "created_at": created_at,
        "timezone": "Asia/Hong_Kong",
        "status": "discovery_completed",
        "trace": [
            {"step": "validate_query", "status": "completed", "query": query.to_dict()},
            {"step": "read_catalogue_snapshot", "status": "completed", "sha256": snapshot.sha256,
             "records": len(snapshot.records), "retrieved_at": snapshot.retrieved_at},
            {"step": "filter_publication_events", "status": "completed", "matches": len(result["updates"])},
            {"step": "prepare_comparison_tasks", "status": "completed", "full_texts_available": False},
        ],
        "comparisons": [],
        "comparison_attempts": [],
        "limitations": [
            "This run covers ISO catalogue publications only. IEC, ASTM, laws and regulatory recognition are not connected.",
            "Current catalogue metadata is not a complete history of confirmations, withdrawals or corrected versions.",
            "A publication does not by itself establish an applicable regulatory obligation or transition deadline.",
            "Missing text, missing predecessor or missing records must not be interpreted as no changes.",
        ],
    })
    return result


def compare_discovered_pair(
    report: dict[str, Any], snapshot: CatalogueSnapshot, *,
    update_id: int, predecessor_id: int, old_path: Path, new_path: Path,
) -> dict[str, Any]:
    """Compare an explicitly selected, catalogue-linked pair without guessing names.

    Identity is pinned to publication IDs selected by the user. The report still
    requires a reviewer to verify that the file contents match those publications.
    Supplements require application to a base text, which this MVP does not invent.
    """
    update = next((row for row in report["updates"] if row["id"] == update_id), None)
    if report.get("source", {}).get("sha256") != snapshot.sha256:
        raise ValueError("The comparison catalogue does not match this discovery report's source snapshot.")
    if update is None:
        raise ValueError("Select an update present in this discovery result.")
    if predecessor_id == update_id:
        raise ValueError("Old and new publications must have distinct catalogue IDs.")
    if predecessor_id not in update.get("replaces", []):
        raise ValueError("The selected old publication is not an official predecessor of this update.")
    old = snapshot.records.get(predecessor_id)
    new = snapshot.records.get(update_id)
    if old is None or new is None:
        raise ValueError("Both publication IDs must be present in the catalogue snapshot.")
    if predecessor_id not in new.replaces:
        raise ValueError("The supplied snapshot does not verify the selected predecessor relationship.")
    if old.supplement_type or new.supplement_type:
        raise ValueError("A supplement needs its base edition and an amendment application workflow; whole-edition comparison is unavailable.")
    if old.publication_date is None or new.publication_date is None:
        raise ValueError("Drafts and publications with unknown publication dates cannot establish this comparison pair.")
    if old.publication_date > new.publication_date:
        raise ValueError("The official relationship has inconsistent chronology and requires review.")
    comparison = compare_standard_files(
        old_path, new_path, old_reference=old.reference, new_reference=new.reference,
    )
    comparison["attempt_id"] = uuid4().hex
    comparison["compared_at"] = datetime.now(timezone.utc).isoformat()
    comparison["catalogue_link"] = {
        "old_id": predecessor_id, "new_id": update_id,
        "old_reference": old.reference, "new_reference": new.reference,
        "relationship": "replaces", "snapshot_sha256": snapshot.sha256,
        "old_source_url": f"https://www.iso.org/standard/{predecessor_id}.html",
        "new_source_url": f"https://www.iso.org/standard/{update_id}.html",
        "file_identity_verification": "user_selected_publication_ids; document contents require reviewer verification",
    }
    comparison.setdefault("warnings", []).append(
        "File names do not prove publication identity. Verify the cover, edition, language and applicable amendments against the selected IDs."
    )
    if len(update.get("replaces", [])) > 1:
        comparison["warnings"].append(
            "This update replaces several publications. This comparison covers only the selected predecessor, not the complete replacement set."
        )
    report["comparisons"] = [
        item for item in report.get("comparisons", [])
        if (item.get("catalogue_link", {}).get("old_id"), item.get("catalogue_link", {}).get("new_id"))
        != (predecessor_id, update_id)
    ] + [comparison]
    report.setdefault("comparison_attempts", []).append(comparison)
    pair_results = [item for item in report["comparisons"] if item["catalogue_link"]["new_id"] == update_id]
    aggregate_status = "partial_replacement_set_review" if len(update.get("replaces", [])) > 1 else comparison["status"]
    update["content_comparison"] = {
        "status": aggregate_status,
        "pairs": [{"predecessor_id": item["catalogue_link"]["old_id"], "status": item["status"],
                   "candidate_changes": len(item.get("changes", [])), "attempt_id": item["attempt_id"]}
                  for item in pair_results],
        "candidate_changes": sum(len(item.get("changes", [])) for item in pair_results),
        "complete_replacement_set": False,
    }
    update["comparison_status"] = aggregate_status
    report["coverage"]["supplied_text_comparison_attempted"] = True
    report["coverage"]["content_comparison_runs"] = len(report["comparisons"])
    report["coverage"]["full_text_comparison_complete"] = False
    report["trace"].append({
        "step": "compare_selected_files", "old_id": predecessor_id, "new_id": update_id,
        "status": comparison["status"], "candidate_changes": len(comparison.get("changes", [])),
        "attempt_id": comparison["attempt_id"], "compared_at": comparison["compared_at"],
        "old_file_sha256": comparison.get("old_document", {}).get("sha256"),
        "new_file_sha256": comparison.get("new_document", {}).get("sha256"),
    })
    return comparison


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def report_markdown(report: dict[str, Any]) -> str:
    """A source-backed report without model-generated content or suppressed rows."""
    query = report.get("query", {})
    source = report.get("source", {})
    lines = [
        "# Standards update review", "",
        f"Run: `{report.get('run_id', '')}` · Created: {report.get('created_at', '')}", "",
        f"Query: `{json.dumps(query, ensure_ascii=False)}`", "",
        "## Source and coverage", "",
        f"Source: {source.get('source_url', source.get('url', 'ISO Open Data'))}", "",
        f"Snapshot SHA-256: `{source.get('sha256', '')}`", "",
        f"Retrieved at: {source.get('retrieved_at', 'unknown')} · Source updated at: {source.get('dataset_updated_at') or 'unknown'}", "",
        "This work is based on the iso_deliverables_metadata dataset from ISO Open Data, licensed under ODC Attribution License (ODC-By) v1.0.", "",
        "- Dataset: https://www.iso.org/open-data.html#iso_deliverables_metadata",
        "- License: https://opendatacommons.org/licenses/by/1-0/", "",
        "```json", json.dumps(report.get("coverage", {}), ensure_ascii=False, indent=2), "```", "",
        f"## Publications found ({len(report.get('updates', []))})", "",
        "Dates below are publication-event dates. Current stage is shown separately; it is not a historical event date.", "",
        "| Publication | Publication date | Event | Predecessor(s) | Current stage | Content comparison |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report.get("updates", []):
        predecessors = "; ".join(str(p.get("reference") or p.get("id")) for p in row.get("predecessors", []))
        ref = f"[{_cell(row['reference'])}]({row['source_url']})"
        fields = [ref, row.get("publication_date", ""), row.get("event_type", ""), predecessors,
                  row.get("current_stage", ""), row.get("content_comparison", {}).get("status", "not_run")]
        lines.append("| " + " | ".join(_cell(value) for value in fields) + " |")
    relationship_notes = [(row["reference"], warning) for row in report.get("updates", [])
                          for warning in row.get("relationship_warnings", [])]
    if relationship_notes:
        lines += ["", "Relationship notes:", ""]
        lines += [f"- {_cell(reference)}: {_cell(warning)}" for reference, warning in relationship_notes]
    lines += ["", "## Candidate content differences", ""]
    if not report.get("comparisons"):
        lines += ["No full-text comparison has been run. Catalogue facts alone do not identify clause-level changes.", ""]
    for result in report.get("comparisons", []):
        pair = result.get("catalogue_link", {})
        lines += [f"### {_cell(pair.get('old_reference', 'Old'))} → {_cell(pair.get('new_reference', 'New'))}", "",
                  f"Status: **{result.get('status', 'unknown')}**", "",
                  f"Old file SHA-256: `{result.get('old_document', {}).get('sha256', '')}`", "",
                  f"New file SHA-256: `{result.get('new_document', {}).get('sha256', '')}`", "",
                  f"Comparison attempt: `{result.get('attempt_id', '')}` · {result.get('compared_at', '')}", ""]
        for change in result.get("changes", []):
            lines += [f"#### Candidate {_cell(change.get('id', ''))}", "",
                      f"- Old: {_cell(change.get('old_clause', ''))} · {_cell(change.get('old_locator', ''))}",
                      f"- New: {_cell(change.get('new_clause', ''))} · {_cell(change.get('new_locator', ''))}",
                      f"- Type: {_cell(change.get('change_type', ''))}",
                      f"- Critical tokens: {_cell(change.get('critical_tokens_changed', []))}", "",
                      "Old text:", "", *["> " + t for t in str(change.get("old_text", "")).splitlines()], "",
                      "New text:", "", *["> " + t for t in str(change.get("new_text", "")).splitlines()], ""]
        lines += ["Comparison limitations:", ""] + ["- " + str(w) for w in result.get("warnings", [])] + [""]
    lines += ["## Limitations and unresolved work", ""]
    lines += ["- " + str(w) for w in report.get("warnings", []) + report.get("limitations", [])]
    return "\n".join(lines) + "\n"
