from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.regulation_identity import (
    RegulationIdentityError,
    normalize_identifier,
    resolve_regulation_pair,
)


class RegulationIdentityTests(unittest.TestCase):
    def test_filename_match_orders_swapped_editions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old = self._text_file(root, "ISO 13485-2003.txt")
            latest = self._text_file(root, "ISO 13485-2016.txt")

            result = resolve_regulation_pair(latest, old)

            self.assertEqual(result.old.file_path, str(old.resolve()))
            self.assertEqual(result.latest.file_path, str(latest.resolve()))
            self.assertEqual(result.family, "ISO 13485")
            self.assertEqual(result.match_mode, "filename_match")
            self.assertEqual(result.aliases, ("ISO 13485",))
            json.dumps(result.to_dict())

    def test_gn_filenames_match_without_reading_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old = self._text_file(root, "stored-a.txt")
            latest = self._text_file(root, "stored-b.txt")

            result = resolve_regulation_pair(
                old,
                latest,
                first_name="GN-02-2025(C).pdf",
                second_name="GN-02-2026(C).pdf",
            )

            self.assertEqual(result.family, "GN-02(C)")
            self.assertEqual(result.old_year, 2025)
            self.assertEqual(result.latest_year, 2026)
            self.assertEqual(result.aliases, ("GN-02(C)", "GN-02"))

    def test_mismatched_filename_names_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = self._text_file(root, "GN-02-2025(C).txt")
            second = self._text_file(root, "GN-03-2026(C).txt")

            with self.assertRaisesRegex(RegulationIdentityError, "do not share the same regulation name") as caught:
                resolve_regulation_pair(first, second)

            self.assertEqual(caught.exception.code, "filename_mismatch")

    def test_missing_year_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = self._text_file(root, "GN-02-old.txt")
            second = self._text_file(root, "GN-02-2026.txt")

            with self.assertRaises(RegulationIdentityError) as caught:
                resolve_regulation_pair(first, second)

            self.assertEqual(caught.exception.code, "year_missing")

    def test_same_edition_year_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = self._text_file(root, "GN-02-2026(C).txt")
            second = self._text_file(root, "GN-02-2026(C)-copy.txt")

            with self.assertRaises(RegulationIdentityError) as caught:
                resolve_regulation_pair(first, second)

            self.assertEqual(caught.exception.code, "same_edition")

    def test_normalization_handles_unicode_sar_prefix_and_copy_suffix(self) -> None:
        self.assertEqual(
            normalize_identifier(" SAR－00151　ISO 13485：2016（1）.PDF "),
            "ISO 13485:2016",
        )

    @staticmethod
    def _text_file(root: Path, name: str) -> Path:
        path = root / name
        path.write_text("Content is intentionally irrelevant to filename matching.", encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
