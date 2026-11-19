"""End-to-end invariants for catalogue identity and comparison evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from modules.standards_catalogue import load_iso_catalogue
from modules.standards_query import QuerySpec
from modules.standards_update_workflow import compare_discovered_pair, report_json, report_markdown, run_update_workflow


def record(identifier, reference, published, *, replaces=None, supplement=None):
    return {"id": identifier, "reference": reference, "title": {"en": "Synthetic specification"},
            "deliverableType": "IS", "supplementType": supplement, "publicationDate": published,
            "edition": 1 if identifier == 1 else 2, "icsCode": ["11.040"], "ownerCommittee": "ISO/TC 999",
            "currentStage": 6060, "replaces": replaces, "replacedBy": None}


class StandardsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        rows = [record(1, "ISO 99999:2025", "2025-06-01"),
                record(2, "ISO 99999:2026", "2026-06-01", replaces=[1]),
                record(3, "ISO 99999:2026/Amd 1:2026", "2026-08-01", replaces=[1], supplement="Amd")]
        self.catalogue = self.root / "synthetic.jsonl"
        self.catalogue.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        self.snapshot = load_iso_catalogue(self.catalogue, retrieved_at="2026-11-19T10:00:00+08:00")
        self.query = QuerySpec(date(2026, 1, 1), date(2026, 11, 19), standards=("ISO 99999",))
        self.report = run_update_workflow(self.snapshot, self.query)
        self.old_path, self.new_path = self.root / "old.txt", self.root / "new.txt"
        self.old_path.write_text("1 Specification\nThe device shall have a clearance of < 5 mm.\n", encoding="utf-8")
        self.new_path.write_text("1 Specification\nThe device shall not have a clearance of > 8 mm.\n", encoding="utf-8")

    def compare(self, **overrides):
        args = dict(update_id=2, predecessor_id=1, old_path=self.old_path, new_path=self.new_path)
        args.update(overrides)
        return compare_discovered_pair(self.report, self.snapshot, **args)

    def test_discovery_never_invents_content_and_supplement_is_separate(self):
        self.assertEqual({r["id"] for r in self.report["updates"]}, {2, 3})
        self.assertEqual(self.report["comparisons"], [])
        self.assertTrue(all(r["content_comparison"]["status"] == "not_run" for r in self.report["updates"]))
        self.assertIn("base edition", self.report["updates"][0]["next_action"])

    def test_linked_pair_preserves_critical_change_and_source_identity_in_exports(self):
        result = self.compare()
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["catalogue_link"]["snapshot_sha256"], self.snapshot.sha256)
        self.assertTrue({"number", "operator", "negation"}.issubset(result["changes"][0]["critical_tokens_changed"]))
        exported = json.loads(report_json(self.report))
        self.assertEqual(exported["comparisons"][0]["catalogue_link"]["new_id"], 2)
        self.assertIn("ODC Attribution", report_markdown(self.report))
        self.assertIn("The device shall not", report_markdown(self.report))
        self.assertIn(self.snapshot.sha256, report_markdown(self.report))
        self.assertFalse(exported["coverage"]["full_text_comparison_complete"])

    def test_cannot_compare_unrelated_publication_or_wrong_snapshot(self):
        with self.assertRaisesRegex(ValueError, "official predecessor"):
            self.compare(predecessor_id=999)
        with self.assertRaisesRegex(ValueError, "present in this discovery"):
            self.compare(update_id=999)
        with self.assertRaisesRegex(ValueError, "source snapshot"):
            compare_discovered_pair(self.report, replace(self.snapshot, sha256="different"),
                                    update_id=2, predecessor_id=1, old_path=self.old_path, new_path=self.new_path)

    def test_amendment_is_not_treated_as_a_complete_new_edition(self):
        with self.assertRaisesRegex(ValueError, "supplement"):
            self.compare(update_id=3)

    def test_extraction_failure_stays_incomplete_in_top_level_report(self):
        self.new_path.write_text("", encoding="utf-8")
        result = self.compare()
        self.assertEqual(result["status"], "comparison_incomplete")
        row = next(r for r in self.report["updates"] if r["id"] == 2)
        self.assertEqual(row["comparison_status"], "comparison_incomplete")
        self.assertEqual(row["content_comparison"]["status"], "comparison_incomplete")

    def test_rerun_replaces_pair_evidence_without_losing_execution_trace(self):
        self.compare()
        self.compare()
        self.assertEqual(len(self.report["comparisons"]), 1)
        self.assertEqual(len(self.report["comparison_attempts"]), 2)
        self.assertEqual(len([x for x in self.report["trace"] if x["step"] == "compare_selected_files"]), 2)

    def test_each_run_is_distinct_but_query_fingerprint_is_stable(self):
        second = run_update_workflow(self.snapshot, self.query)
        self.assertNotEqual(self.report["run_id"], second["run_id"])
        self.assertEqual(self.report["query_fingerprint"], second["query_fingerprint"])

    def test_multiple_predecessors_keep_aggregate_status_partial(self):
        records = dict(self.snapshot.records)
        records[4] = replace(records[1], id=4, reference="ISO 99999:2024")
        records[2] = replace(records[2], replaces=(1, 4))
        self.snapshot = replace(self.snapshot, records=records)
        self.report = run_update_workflow(self.snapshot, self.query)
        self.compare()
        self.compare(predecessor_id=4)
        row = next(r for r in self.report["updates"] if r["id"] == 2)
        self.assertEqual(row["comparison_status"], "partial_replacement_set_review")
        self.assertEqual(len(row["content_comparison"]["pairs"]), 2)
        self.assertFalse(row["content_comparison"]["complete_replacement_set"])


if __name__ == "__main__":
    unittest.main()
