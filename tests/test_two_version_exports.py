from __future__ import annotations

import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.export_utils import audit_workbook_bytes
from modules.report_generator import generate_two_version_report


class TwoVersionExportTests(unittest.TestCase):
    def test_audit_workbook_contains_named_styled_sheets(self) -> None:
        payload = audit_workbook_bytes(
            {
                "Input Match": pd.DataFrame([{"family": "ISO 13485", "rows": [18, 147]}]),
                "Affected Files": pd.DataFrame([{"document_code": "QSP0809", "depth": 0}]),
            }
        )
        workbook = load_workbook(BytesIO(payload))

        self.assertEqual(workbook.sheetnames, ["Input Match", "Affected Files"])
        self.assertEqual(workbook["Input Match"].freeze_panes, "A2")
        self.assertEqual(workbook["Input Match"]["A1"].fill.fgColor.rgb[-6:], "0F766E")

    def test_two_version_report_is_created(self) -> None:
        analysis = {
            "resolved_pair": {
                "family_key": "ISO 13485",
                "old_year": 2003,
                "latest_year": 2016,
                "match_mode": "filename_match",
            },
            "comparison": {
                "selected_method": "official_exact_edition_comparison",
                "accepted_changes": [{"latest_clause_id": "8.2.2", "official_change_comment": "Complaint handling added."}],
            },
            "dependency": {
                "nodes": [{"document_code": "QSP0809", "depth": 0, "relationship_path": "QSP0809"}],
                "unresolved_references": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_two_version_report(Path(tmpdir) / "report.docx", analysis, [])

            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
