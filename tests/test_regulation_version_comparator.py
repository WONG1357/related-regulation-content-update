from __future__ import annotations

import json
import tempfile
import unittest
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.impact_analyzer import select_candidate_pairs
from modules.regulation_version_comparator import (
    AcceptedClauseChangeEvidence,
    aggregate_clause_comparisons,
    compare_regulation_versions,
    detect_official_comparison_material,
    extract_iso_13485_main_body_clauses,
    extract_normative_clause_blocks,
    write_comparison_audit,
)


class RegulationVersionComparatorTests(unittest.TestCase):
    def test_same_clause_number_changed_text_is_not_unchanged(self) -> None:
        comparison = compare_texts(
            "1 Scope\nThe organization shall maintain a documented procedure for records.",
            "1 Scope\nThe organization shall maintain a documented procedure for records and risk controls.",
        )

        classifications = classifications_by_latest(comparison)
        self.assertNotEqual(classifications["1"], "unchanged")
        self.assertIn(classifications["1"], {"expanded_requirement", "modified_requirement"})

    def test_added_deleted_and_renumbered_clauses_classify(self) -> None:
        comparison = compare_texts(
            "1 Scope\nThe organization shall maintain records.\n2 Procedure\nThe organization shall document a procedure.",
            "1 Scope\nThe organization shall maintain records.\n3 Procedure\nThe organization shall document a procedure.\n4 Software\nThe organization shall validate software.",
        )

        rows = comparison["clause_comparisons"]
        by_latest = classifications_by_latest(comparison)
        by_old = {row["old_clause_id"]: row["change_classification"] for row in rows if row["old_clause_id"]}
        self.assertEqual(by_latest["3"], "renumbered_only")
        self.assertEqual(by_latest["4"], "newly_added_clause")
        self.assertEqual(by_old["2"], "renumbered_only")

    def test_editorial_change_only_classifies(self) -> None:
        comparison = compare_texts(
            "1 Scope\nThe organization shall maintain records.",
            "1 Scope\nThe organisation shall maintain records.",
        )

        self.assertEqual(classifications_by_latest(comparison)["1"], "editorial_change_only")

    def test_unchanged_and_editorial_are_excluded_from_ai_candidates_by_default(self) -> None:
        comparison = compare_texts(
            "1 Scope\nThe organization shall maintain records.",
            "1 Scope\nThe organisation shall maintain records.",
        )
        profile = {
            "source_mode": "old_vs_latest_file_comparison",
            "confidence": "High",
            "exact_clause_verified": True,
            "needs_human_confirmation": True,
            "amendment_summary_used": "Editorial only",
            "affected_qms_topics": [],
            "search_keywords": [],
            "ai_review_change_classes": comparison["reviewable_change_classes"],
        }
        regs = pd.DataFrame(
            [
                {
                    "原标准编号": "ISO 13485:2003",
                    "最新版本": "ISO 13485:2016",
                    "最新年份": "2016",
                    "状态": "已更新",
                    "主要变更内容": "",
                    "查询参考": "",
                }
            ]
        )
        documents = [
            {
                "file_name": "QSP0401 Test.docx",
                "file_path": "/tmp/QSP0401 Test.docx",
                "document_code": "QSP0401",
                "document_title": "Document control",
                "document_type": "QSP",
                "reference_standards": ["ISO 13485"],
                "reference_regulations": [],
                "reference_guidance": [],
                "document_relationships": [],
                "sections": [{"text": "References ISO 13485"}],
            }
        ]

        candidates = select_candidate_pairs(regs, documents, amendment_profiles=[profile])

        self.assertFalse(bool(candidates.iloc[0]["review_required"]))
        self.assertIn("no substantive", candidates.iloc[0]["skip_reason"])

    def test_iso_13485_extraction_ignores_toc_annex_a_and_annex_b(self) -> None:
        text = """Contents
1 Scope 1
4.2.3 Control of documents 5
Annex B Correspondence between ISO 13485:2016 and ISO 9001:2015 30
INTERNATIONAL STANDARD ISO 13485:2016(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
4.2.3 Medical device file
The organization shall establish and maintain one or more files for each medical device type.
8.5.3 Preventive action
The organization shall document a procedure for preventive action.
Annex A
4.2.3 Medical device file — Added requirement.
Annex B
9.1.1 General
ISO 9001 correspondence only.
10.3 Continual improvement
ISO 9001 correspondence only.
Bibliography
"""

        clauses = extract_iso_13485_main_body_clauses(text, "2016", "fixture.txt")
        clause_ids = [clause.clause_id for clause in clauses]

        self.assertEqual(clause_ids, ["1", "4.2.3", "8.5.3"])
        self.assertNotIn("9.1.1", clause_ids)
        self.assertNotIn("10.3", clause_ids)
        self.assertFalse(any("..." in clause.clause_title for clause in clauses))

    def test_iso_13485_known_renumbering_prevents_bad_exact_clause_mapping(self) -> None:
        comparison = compare_texts(
            """INTERNATIONAL STANDARD ISO 13485:2003(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
4.2.3 Control of documents
A documented procedure shall be established to control quality management system documents.
4.2.4 Control of records
Records shall be established and maintained to provide evidence of conformity.
7.3.7 Control of design and development changes
Design and development changes shall be identified and records maintained.
""",
            """INTERNATIONAL STANDARD ISO 13485:2016(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
4.2.3 Medical device file
The organization shall establish and maintain one or more files for each medical device type.
4.2.4 Control of documents
A documented procedure shall be established to control quality management system documents.
4.2.5 Control of records
Records shall be established and maintained to provide evidence of conformity.
7.3.9 Control of design and development changes
Design and development changes shall be identified and records maintained.
Annex A
Comparison summary.
""",
        )

        rows = comparison["clause_comparisons"]
        by_latest = {row["latest_clause_id"]: row for row in rows if row["latest_clause_id"]}

        self.assertEqual(by_latest["4.2.4"]["old_clause_id"], "4.2.3")
        self.assertEqual(by_latest["4.2.4"]["mapping_method"], "known_iso_13485_renumbering")
        self.assertEqual(by_latest["4.2.5"]["old_clause_id"], "4.2.4")
        self.assertEqual(by_latest["7.3.9"]["old_clause_id"], "7.3.7")
        self.assertEqual(by_latest["4.2.3"]["change_classification"], "newly_added_clause")
        self.assertNotEqual(by_latest["4.2.3"]["old_clause_id"], "4.2.3")

    def test_iso_13485_exact_clause_number_requires_similar_title(self) -> None:
        comparison = compare_texts(
            """INTERNATIONAL STANDARD ISO 13485:2003(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
6.4 Work environment
The organization shall determine and manage the work environment needed.
""",
            """INTERNATIONAL STANDARD ISO 13485:2016(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
6.4 Work environment and contamination control
The organization shall determine and manage the work environment and contamination controls needed.
Annex A
Comparison summary.
""",
        )

        row = next(row for row in comparison["clause_comparisons"] if row["latest_clause_id"] == "6.4")

        self.assertNotEqual(row["mapping_method"], "exact_clause_number")

    def test_iso_13485_duplicate_clause_titles_fail_closed(self) -> None:
        comparison = compare_texts(
            """INTERNATIONAL STANDARD ISO 13485:2003(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
""",
            """INTERNATIONAL STANDARD ISO 13485:2016(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
4.2.3 Medical device file
The organization shall establish and maintain one or more files for each medical device type.
4.2.3 Control of documents
The organization shall document controls for documents.
Annex A
4.2.3 Medical device file — Added requirement.
""",
        )

        self.assertTrue(comparison["comparison_validation_failed"])
        self.assertEqual(comparison["clause_comparisons"], [])
        self.assertTrue(any("multiple body titles" in failure for failure in comparison["validation_failures"]))

    def test_iso_13485_known_2016_title_mismatch_fails_closed(self) -> None:
        comparison = compare_texts(
            """INTERNATIONAL STANDARD ISO 13485:2003(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
""",
            """INTERNATIONAL STANDARD ISO 13485:2016(E)
1 Scope
The organization shall maintain a quality management system for medical devices.
4.2.3 Control of documents
The organization shall document controls for documents.
Annex A
Comparison summary.
""",
        )

        self.assertTrue(comparison["comparison_validation_failed"])
        self.assertTrue(any("4.2.3" in failure and "Medical device file" in failure for failure in comparison["validation_failures"]))

    def test_aggregate_clause_comparisons_keeps_strongest_classification(self) -> None:
        rows = [
            {
                "old_clause_id": "1",
                "latest_clause_id": "1",
                "change_classification": "unchanged",
                "added_requirement_units": [],
                "affected_qms_topics": [],
                "evidence_preview": "unchanged",
            },
            {
                "old_clause_id": "1",
                "latest_clause_id": "1",
                "change_classification": "modified_requirement",
                "added_requirement_units": ["The organization shall document risk controls."],
                "affected_qms_topics": ["risk-based QMS approach"],
                "evidence_preview": "modified",
            },
        ]

        aggregated = aggregate_clause_comparisons(rows)

        self.assertEqual(len(aggregated), 1)
        self.assertEqual(aggregated[0]["change_classification"], "modified_requirement")
        self.assertIn("unchanged", aggregated[0]["pre_aggregation_classifications"])

    def test_validation_failure_blocks_comparison_audit_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError):
                write_comparison_audit(
                    Path(tmpdir),
                    {
                        "comparison_validation_failed": True,
                        "validation_failures": ["polluted extraction"],
                    },
                )

    def test_exact_edition_official_comparison_is_selected_as_primary(self) -> None:
        comparison = compare_texts(
            """INTERNATIONAL STANDARD ISO 13485:2003(E)
1 Scope
The organization shall maintain documented procedures and records for its quality management system.
4.1 General requirements
The organization shall document the quality management system processes needed by the organization.
""",
            """INTERNATIONAL STANDARD ISO 13485:2016(E)
1 Scope
The organization shall maintain documented procedures and records for its quality management system. The organization shall document risk-control decisions in the medical device file.
4.1 General requirements
The organization shall document its roles and apply a risk-based approach to quality management system processes.
Annex A
Comparison of content between ISO 13485:2003 and ISO 13485:2016
Clause in ISO 13485:2016 Comment on change compared with ISO 13485:2003
4.1 General requirements — Added requirements for organization roles and a risk-based approach.
""",
        )

        self.assertEqual(comparison["selected_method"], "exact_edition_official_comparison")
        self.assertTrue(comparison["official_comparison"]["usable_as_primary"])
        accepted = next(row for row in comparison["accepted_changes"] if row["latest_clause_id"] == "4.1")
        self.assertEqual(accepted["evidence_method"], "exact_edition_official_comparison")
        self.assertIn("risk-based approach", accepted["official_change_summary"])

    def test_wrong_edition_official_comparison_is_rejected(self) -> None:
        comparison = compare_texts(
            """INTERNATIONAL STANDARD ISO 14971:2000(E)
1 Scope
The manufacturer shall establish a process for identifying hazards associated with medical devices.
""",
            """INTERNATIONAL STANDARD ISO 14971:2019(E)
1 Scope
The manufacturer shall establish, document, and maintain an ongoing process for identifying hazards and controlling risks.
Annex B
Correspondence between ISO 14971:2007 and ISO 14971:2019
ISO 14971:2007 ISO 14971:2019
3.1 Risk analysis 4.1 Risk management process
""",
            regulation_id="ISO 14971",
            old_version="2000",
            latest_version="2019",
        )

        self.assertEqual(comparison["selected_method"], "direct_normative_clause_comparison")
        self.assertTrue(comparison["official_comparison"]["detected"])
        self.assertFalse(comparison["official_comparison"]["usable_as_primary"])
        self.assertIn("uploaded editions", comparison["official_comparison"]["rejection_reason"])

    def test_exact_edition_official_rows_not_in_normative_body_are_rejected(self) -> None:
        comparison = compare_texts(
            """INTERNATIONAL STANDARD ISO 13485:2003(E)
1 Scope
The organization shall maintain documented procedures and records for its quality management system.
""",
            """INTERNATIONAL STANDARD ISO 13485:2016(E)
1 Scope
The organization shall maintain documented procedures and records for its quality management system. The organization shall document risk-control decisions in the medical device file.
Annex A
Comparison of content between ISO 13485:2003 and ISO 13485:2016
Clause in ISO 13485:2016 Comment on change compared with ISO 13485:2003
99.1 Fabricated row — Added a requirement that is not present in the normative body.
""",
        )

        self.assertEqual(comparison["selected_method"], "direct_normative_clause_comparison")
        self.assertFalse(comparison["official_comparison"]["usable_as_primary"])
        self.assertIn("normative clauses", comparison["official_comparison"]["rejection_reason"])
        self.assertEqual(len(comparison["accepted_changes"]), 1)

    def test_direct_normative_comparison_is_used_without_official_material(self) -> None:
        comparison = compare_texts(
            "1 Scope\nThe organization shall maintain documented procedures and records for product realization.",
            "1 Scope\nThe organization shall maintain documented procedures, records, and risk controls for product realization.",
            regulation_id="ISO 9001",
            old_version="2015",
            latest_version="2026",
        )

        self.assertEqual(comparison["selected_method"], "direct_normative_clause_comparison")
        self.assertEqual(len(comparison["accepted_changes"]), 1)
        self.assertEqual(comparison["accepted_changes"][0]["evidence_method"], "direct_normative_clause_comparison")

    def test_official_and_normative_conflict_is_excluded_from_accepted_changes(self) -> None:
        unchanged = "The organization shall maintain documented procedures and records for its quality management system."
        comparison = compare_texts(
            f"INTERNATIONAL STANDARD ISO 13485:2003(E)\n1 Scope\n{unchanged}",
            f"""INTERNATIONAL STANDARD ISO 13485:2016(E)
1 Scope
{unchanged}
Annex A
Comparison of content between ISO 13485:2003 and ISO 13485:2016
Clause in ISO 13485:2016 Comment on change compared with ISO 13485:2003
1 Scope — Added a new mandatory risk-control requirement.
""",
        )

        row = next(row for row in comparison["clause_comparisons"] if row["latest_clause_id"] == "1")
        self.assertTrue(row["evidence_conflict"])
        self.assertTrue(row["needs_human_confirmation"])
        self.assertFalse(row["accepted_for_drafting"])
        self.assertEqual(comparison["accepted_changes"], [])

    def test_coordinate_table_rows_are_parsed_for_exact_editions(self) -> None:
        official = detect_official_comparison_material(
            "Annex A\nComparison of content between ISO 13485:2003 and ISO 13485:2016",
            regulation_id="ISO 13485",
            old_version="2003",
            latest_version="2016",
            table_records=[
                {
                    "page_number": 35,
                    "section_context": "Annex A Comparison of content between ISO 13485:2003 and ISO 13485:2016",
                    "page_text": "Table A.1 — Comparison of content between ISO 13485:2003 and ISO 13485:2016",
                    "rows": [
                        ["Clause in ISO 13485:2016", "Comment on change compared with ISO 13485:2003"],
                        ["4.1 General requirements", "Added requirement to apply a risk-based approach."],
                        ["", "The role of the organization shall also be documented."],
                    ],
                }
            ],
        )

        self.assertTrue(official["usable_as_primary"])
        self.assertEqual(len(official["mapping_rows"]), 1)
        self.assertEqual(official["mapping_rows"][0]["latest_clause_id"], "4.1")
        self.assertIn("role of the organization", official["mapping_rows"][0]["change_summary"])
        self.assertEqual(official["mapping_rows"][0]["source_kind"], "pdf_table")

    def test_editorial_and_automatic_unmatched_rows_are_not_accepted(self) -> None:
        comparison = compare_texts(
            """1 Scope
The organization shall maintain documented procedures and records for its quality management system.
2 Obsolete retention
The manufacturer shall archive paper certificates in a locked storage room for seventy years.
""",
            """1 Scope
The organisation shall maintain documented procedures and records for its quality management system.
3 Cloud automation
The provider shall execute ephemeral compute jobs without retaining medical device quality records.
""",
            regulation_id="ISO 9001",
            old_version="2015",
            latest_version="2026",
        )

        classifications = {row["change_classification"] for row in comparison["clause_comparisons"]}
        self.assertIn("editorial_change_only", classifications)
        self.assertIn("newly_added_clause", classifications)
        self.assertIn("deleted_clause", classifications)
        self.assertEqual(comparison["accepted_changes"], [])

    def test_generic_clause_extraction_excludes_front_matter_and_annexes(self) -> None:
        clauses = extract_normative_clause_blocks(
            """Foreword
This front matter mentions 9.1 but is not normative body content.
1 Scope
The organization shall apply this document to its quality management system and maintain records.
2 Normative references
The referenced documents shall be applied when their requirements are applicable.
Annex A
9.1 Informative example
This annex text shall not be included in the normative clause comparison.
""",
            "2026",
            "fixture.txt",
        )

        self.assertEqual([clause.clause_id for clause in clauses], ["1", "2"])

    def test_accepted_change_model_serializes_with_deterministic_evidence_id(self) -> None:
        old_text = "1 Scope\nThe organization shall maintain documented procedures and records for product realization."
        latest_text = (
            "1 Scope\nThe organization shall maintain documented procedures and records for product realization. "
            "The organization shall document risk controls before product release."
        )
        first = compare_texts(old_text, latest_text, regulation_id="ISO 9001", old_version="2015", latest_version="2026")
        second = compare_texts(old_text, latest_text, regulation_id="ISO 9001", old_version="2015", latest_version="2026")

        first_row = first["accepted_changes"][0]
        second_row = second["accepted_changes"][0]
        self.assertEqual(first_row["evidence_id"], second_row["evidence_id"])
        self.assertRegex(first_row["evidence_id"], r"^ace-[0-9a-f]{20}$")
        model = AcceptedClauseChangeEvidence.from_row(first_row)
        self.assertEqual(model.to_dict(), first_row)
        self.assertIsInstance(json.dumps(model.to_dict()), str)


def compare_texts(
    old_text: str,
    latest_text: str,
    regulation_id: str = "ISO_13485",
    old_version: str = "2003",
    latest_version: str = "2016",
):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        old_file = root / "old.txt"
        latest_file = root / "latest.txt"
        old_file.write_text(old_text, encoding="utf-8")
        latest_file.write_text(latest_text, encoding="utf-8")
        return compare_regulation_versions(old_file, latest_file, regulation_id, old_version, latest_version)


def classifications_by_latest(comparison: dict) -> dict[str, str]:
    return {
        row["latest_clause_id"]: row["change_classification"]
        for row in comparison["clause_comparisons"]
        if row["latest_clause_id"]
    }


if __name__ == "__main__":
    unittest.main()
