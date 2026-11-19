"""Regression cases for material changes that similarity-only diff can miss."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from modules.standards_diff import compare_standard_files


class StandardsDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def compare(self, old: str, new: str) -> dict:
        first, second = self.root / "old.txt", self.root / "new.txt"
        first.write_text(old, encoding="utf-8")
        second.write_text(new, encoding="utf-8")
        return compare_standard_files(first, second, old_reference="ISO TEST:2025", new_reference="ISO TEST:2026")

    def test_material_one_token_changes_are_never_editorial_or_unchanged(self) -> None:
        cases = [
            ("5 mm", "8 mm", "number"),
            ("shall release", "shall not release", "negation"),
            ("should document", "shall document", "modality"),
            ("1 mg", "1 µg", "unit"),
            ("< 5 percent", "> 5 percent", "operator"),
            ("≤ 5 percent", "≥ 5 percent", "operator"),
        ]
        for before, after, category in cases:
            with self.subTest(category=category, before=before):
                prefix = "1 Scope\nThe manufacturer "
                suffix = " when all specified instrument tests are complete."
                result = self.compare(prefix + before + suffix, prefix + after + suffix)
                self.assertEqual(result["status"], "review_required")
                self.assertEqual(len(result["changes"]), 1)
                row = result["changes"][0]
                self.assertEqual(row["change_type"], "critical_text_change")
                self.assertIn(category, row["critical_tokens_changed"])
                self.assertIn(before, row["old_text"])
                self.assertIn(after, row["new_text"])
                self.assertTrue(row["review_required"])

    def test_reordered_numeric_bounds_remain_critical(self) -> None:
        result = self.compare("1 Scope\nThe value shall be greater than 5 and less than 8.",
                              "1 Scope\nThe value shall be greater than 8 and less than 5.")
        self.assertIn("number", result["changes"][0]["critical_tokens_changed"])

    def test_normative_and_unknown_annexes_remain_in_scope(self) -> None:
        old = "1 Scope\nThe test applies.\nAnnex A (normative)\nA.1 Limit\nThe value shall be < 5 mg.\nAnnex B\nB.1 Record\nRetain 5 records."
        result = self.compare(old, old.replace("< 5 mg", "< 8 mg").replace("5 records", "7 records"))
        changes = {row["new_clause"]: row for row in result["changes"]}
        self.assertEqual(changes["A.1"]["new_role"], "normative")
        self.assertEqual(changes["B.1"]["new_role"], "unspecified")
        self.assertTrue(result["coverage"]["unknown_annex_roles"])

    def test_annex_role_on_separate_line_is_recognized(self) -> None:
        old = "Annex A\n(normative)\nA.1 Limit\nThe value shall be 5 mg."
        result = self.compare(old, old.replace("5 mg", "8 mg"))
        self.assertEqual(result["changes"][0]["new_role"], "normative")

    def test_unrelated_text_at_same_clause_number_is_uncertain(self) -> None:
        result = self.compare("1 General\nSatellites transmit radio signals through orbital communication relays.",
                              "1 General\nSterile sutures require validated packaging processes and biological evaluation.")
        self.assertEqual(result["changes"][0]["change_type"], "mapping_uncertain")

    def test_unique_renumbering_is_a_review_candidate(self) -> None:
        old = "4.1 Records\nThe manufacturer shall retain documented evidence for every measurement."
        result = self.compare(old, old.replace("4.1 Records", "5.2 Records"))
        self.assertEqual(result["changes"][0]["change_type"], "renumbering_candidate")
        self.assertEqual(result["changes"][0]["critical_tokens_changed"], [])

    def test_unmatched_sections_are_not_asserted_to_be_added_or_deleted(self) -> None:
        result = self.compare("1 Scope\nA common scope.\n2 Sampling\nUse the prescribed sampling approach.",
                              "1 Scope\nA common scope.\n3 Identification\nIdentify each labeled sample.")
        self.assertEqual(len(result["changes"]), 2)
        self.assertEqual({row["change_type"] for row in result["changes"]}, {"unmatched_or_split_merge"})

    def test_reordered_sections_are_not_silently_reported_unchanged(self) -> None:
        first = "1 Sampling\nCollect the sample according to this procedure."
        second = "2 Measurement\nMeasure the sample according to this procedure."
        result = self.compare(first + "\n" + second, second + "\n" + first)
        self.assertEqual(result["status"], "review_required")
        self.assertEqual({row["change_type"] for row in result["changes"]}, {"relocation_candidate"})

    def test_same_file_and_identical_bytes_are_rejected(self) -> None:
        source = self.root / "source.txt"
        source.write_text("1 Scope\nSame bytes.")
        copy = self.root / "copy.txt"
        copy.write_bytes(source.read_bytes())
        for other in [source, copy]:
            with self.subTest(other=other.name):
                result = compare_standard_files(source, other, old_reference="2025", new_reference="2026")
                self.assertEqual(result["status"], "comparison_rejected")
                self.assertEqual(result["old_document"]["sha256"], result["new_document"]["sha256"])

    def test_failed_extraction_never_reports_no_changes(self) -> None:
        for old in ["", " \n\t"]:
            with self.subTest(old=old):
                result = self.compare(old, "1 Scope\nReadable text.")
                self.assertEqual(result["status"], "comparison_incomplete")
                self.assertEqual(result["old_document"]["status"], "error")
        unsupported = self.root / "old.bin"
        unsupported.write_bytes(b"not a supported standard")
        missing = self.root / "missing.txt"
        result = compare_standard_files(unsupported, missing, old_reference="old", new_reference="new")
        self.assertEqual(result["status"], "comparison_incomplete")
        self.assertIn("Unsupported format", result["old_document"]["error"])
        self.assertIn("FileNotFoundError", result["new_document"]["error"])

    def test_no_difference_only_claims_extracted_scope(self) -> None:
        result = self.compare("1 Scope\nUnchanged content.\n", "1 Scope\nUnchanged content.")
        self.assertEqual(result["status"], "no_textual_differences_found")
        self.assertFalse(result["coverage"]["semantic_completeness_verified"])
        self.assertTrue(any("does not establish" in warning for warning in result["warnings"]))

    @unittest.skipUnless(importlib.util.find_spec("docx"), "python-docx optional dependency unavailable")
    def test_docx_tables_are_compared_in_document_order_with_locators(self) -> None:
        from docx import Document
        paths = [self.root / "old.docx", self.root / "new.docx"]
        for path, value in zip(paths, ["5 mg", "8 mg"]):
            document = Document()
            document.add_paragraph("1 Limits")
            document.add_paragraph("Apply the following limit.")
            table = document.add_table(rows=1, cols=2)
            table.cell(0, 0).text = "Maximum residue"
            table.cell(0, 1).text = value
            document.add_paragraph("2 Records")
            document.add_paragraph("Retain the result.")
            document.save(path)
        result = compare_standard_files(*paths, old_reference="old", new_reference="new")
        row = result["changes"][0]
        self.assertEqual(row["new_clause"], "1")
        self.assertIn("8 mg", row["new_text"])
        self.assertIn("table 1, row 1", row["new_locator"])
        self.assertIn("number", row["critical_tokens_changed"])

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf optional dependency unavailable")
    def test_partial_pdf_with_identical_extracted_text_is_incomplete(self) -> None:
        paths = [self.root / "old.pdf", self.root / "new.pdf"]
        paths[0].write_bytes(b"different old PDF bytes")
        paths[1].write_bytes(b"different new PDF bytes")
        page = SimpleNamespace(extract_text=lambda: "1 Scope\nThe value shall be 5 mg.")
        blank = SimpleNamespace(extract_text=lambda: "")
        reader = SimpleNamespace(is_encrypted=False, pages=[page, blank])
        with patch("pypdf.PdfReader", return_value=reader):
            result = compare_standard_files(*paths, old_reference="old", new_reference="new")
        self.assertEqual(result["status"], "comparison_incomplete")
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["old_document"]["empty_pages"], [2])
        self.assertFalse(result["coverage"]["extraction_complete"])

    def test_optional_dependency_error_is_explicit(self) -> None:
        old, new = self.root / "old.pdf", self.root / "new.pdf"
        old.write_bytes(b"old PDF")
        new.write_bytes(b"new PDF")
        with patch.dict("sys.modules", {"pypdf": None}):
            result = compare_standard_files(old, new, old_reference="old", new_reference="new")
        self.assertEqual(result["status"], "comparison_incomplete")
        self.assertIn("optional pypdf package", result["old_document"]["error"])

    def test_output_and_evidence_ids_are_stable_and_json_serializable(self) -> None:
        old, new = "1 Scope\nA limit of 5 mg.", "1 Scope\nA limit of 8 mg."
        first, second = self.compare(old, new), self.compare(old, new)
        self.assertEqual(first["changes"][0]["id"], second["changes"][0]["id"])
        self.assertEqual(json.loads(json.dumps(first)), first)


if __name__ == "__main__":
    unittest.main()
