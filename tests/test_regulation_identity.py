from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from regulatory_update_app.modules.regulation_identity import (
    RegulationIdentityError,
    normalize_identifier,
    resolve_regulation_pair,
)


class RegulationIdentityTests(unittest.TestCase):
    def test_exact_match_orders_swapped_uploads_and_aggregates_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old = self._text_file(root, "SAR-00151  ISO 13485 2003.pdf.txt", "ISO 13485:2003 old edition")
            latest = self._text_file(root, "SAR-00221 ISO 13485-2016(1).txt", "ISO 13485:2016 latest edition")
            workbook = self._workbook(
                root,
                [
                    [18, "BS EN ISO 13485:2003", 2003, "ISO 13485:2016", 2016],
                    [22, "YY/T 0287-2003/ISO 13485", 2003, "ISO 13485:2016", 2016],
                    ["subtotal", "INVALID ISO 13485", 2003, "ISO 13485:2016", 2016],
                ],
            )

            result = resolve_regulation_pair(latest, old, workbook)

            self.assertEqual(result.old.file_path, str(old.resolve()))
            self.assertEqual(result.latest.file_path, str(latest.resolve()))
            self.assertEqual(result.family, "ISO 13485")
            self.assertEqual(result.match_mode, "exact_match")
            self.assertEqual(result.matched_row_numbers, (18, 22))
            self.assertIn("BS EN ISO 13485:2003", result.aliases)
            self.assertNotIn("INVALID ISO 13485", result.aliases)
            json.dumps(result.to_dict())

    def test_family_fallback_accepts_old_edition_absent_from_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old = self._text_file(root, "SAR-00619 ISO 14971-2000.txt", "ISO 14971:2000")
            latest = self._text_file(root, "SAR-00341 ISO 14971-2019.txt", "ISO 14971:2019")
            workbook = self._workbook(
                root,
                [
                    [8, "BS EN ISO 14971", 2009, "ISO 14971:2019", 2019],
                    [82, "BS EN ISO 14971", 2012, "ISO 14971:2019", 2019],
                    [170, "YY/T 0316 / ISO 14971", 2008, "ISO 14971:2019", 2019],
                ],
            )

            result = resolve_regulation_pair(old, latest, workbook)

            self.assertEqual(result.match_mode, "family_match")
            self.assertEqual(result.matched_row_numbers, (8, 82, 170))
            self.assertTrue(result.warnings)
            self.assertIn("YY/T 0316", result.aliases)

    def test_mismatched_families_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = self._text_file(root, "ISO 13485 2003.txt", "ISO 13485:2003")
            second = self._text_file(root, "ISO 14971 2019.txt", "ISO 14971:2019")
            workbook = self._workbook(root, [[1, "ISO 13485", 2003, "ISO 13485:2016", 2016]])

            with self.assertRaisesRegex(RegulationIdentityError, "different regulation families") as caught:
                resolve_regulation_pair(first, second, workbook)

            self.assertEqual(caught.exception.code, "family_mismatch")

    def test_missing_year_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = self._text_file(root, "ISO 13485 old.txt", "ISO 13485 without an edition date")
            second = self._text_file(root, "ISO 13485 2016.txt", "ISO 13485:2016")
            workbook = self._workbook(root, [[1, "ISO 13485", 2003, "ISO 13485:2016", 2016]])

            with self.assertRaises(RegulationIdentityError) as caught:
                resolve_regulation_pair(first, second, workbook)

            self.assertEqual(caught.exception.code, "year_missing")

    def test_ambiguous_latest_group_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old = self._text_file(root, "ISO 13485 2003.txt", "ISO 13485:2003")
            latest = self._text_file(root, "ISO 13485 2016.txt", "ISO 13485:2016")
            workbook = self._workbook(
                root,
                [
                    [1, "ISO 13485:2003", 2003, "ISO 13485:2016", 2016],
                    [2, "BS EN ISO 13485:2003", 2003, "ISO 13485:2016/A1", 2016],
                ],
            )

            with self.assertRaises(RegulationIdentityError) as caught:
                resolve_regulation_pair(old, latest, workbook)

            self.assertEqual(caught.exception.code, "ambiguous_latest_match")

    def test_normalization_handles_unicode_sar_prefix_and_copy_suffix(self) -> None:
        self.assertEqual(
            normalize_identifier(" SAR－00151　ISO 13485：2016（1）.PDF "),
            "ISO 13485:2016",
        )

    @staticmethod
    def _text_file(root: Path, name: str, content: str) -> Path:
        path = root / name
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _workbook(root: Path, rows: list[list[object]]) -> Path:
        path = root / "registry.xlsx"
        frame = pd.DataFrame(
            rows,
            columns=["序号", "原标准编号", "原版本年份", "最新版本", "最新年份"],
        )
        frame.to_excel(path, index=False, sheet_name="原始总表")
        return path


if __name__ == "__main__":
    unittest.main()
