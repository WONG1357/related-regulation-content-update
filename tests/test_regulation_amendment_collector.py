from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.regulation_amendment_collector import (
    build_amendment_profiles,
    collect_regulation_amendments,
    extract_qms_topics_and_keywords,
    generate_queries,
    load_excel_items,
    normalize_identity,
)


class RegulationAmendmentCollectorTests(unittest.TestCase):
    def test_load_excel_items_filters_updated_rows_and_normalizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "updates.xlsx"
            pd.DataFrame(
                [
                    {
                        "原标准编号": "BS EN ISO 13485:2003",
                        "原版本年份": 2003,
                        "最新版本": "ISO 13485:2016",
                        "最新年份": 2016,
                        "状态": "已更新",
                        "主要变更内容": "Updated QMS requirements",
                        "查询参考": "",
                    },
                    {"原标准编号": "ISO 14971:2019", "状态": "当前版本"},
                ]
            ).to_excel(path, index=False, sheet_name="原始总表")

            items = load_excel_items(path, "原始总表")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].normalized_id, "ISO_13485")
        self.assertEqual(items[0].standard_type, "iso")

    def test_normalize_identity_handles_expected_families(self) -> None:
        self.assertEqual(normalize_identity("ASTM F 1929-98", "ASTM F1929-23"), ("ASTM_F1929", "astm"))
        self.assertEqual(normalize_identity("Regulation (EU) 2017/745", ""), ("EU_2017_745", "eu"))
        self.assertEqual(normalize_identity("ISO 594-1:1986", "ISO 80369-7:2021"), ("ISO_594_1", "iso"))

    def test_generate_queries_focuses_on_amendment_evidence(self) -> None:
        item = make_item("ASTM F 1929-98", "ASTM F1929-23")
        queries = generate_queries(item)

        self.assertTrue(any("amendment" in query for query in queries))
        self.assertTrue(any("what changed" in query for query in queries))
        self.assertTrue(any("site:astm.org" in query for query in queries))

    def test_excel_summary_fallback_sets_safe_profile_fields(self) -> None:
        item = make_item("ISO 14971:2019", "ISO 14971:2025", summary="Updated risk management requirements and risk-benefit analysis.")
        with tempfile.TemporaryDirectory() as tmpdir:
            profiles = build_amendment_profiles([item], output_dir=Path(tmpdir), regulation_files_dir=Path(tmpdir) / "missing")

        profile = profiles[0]
        self.assertEqual(profile["source_mode"], "excel_amendment_summary")
        self.assertEqual(profile["confidence"], "Medium")
        self.assertFalse(profile["exact_clause_verified"])
        self.assertTrue(profile["needs_human_confirmation"])
        self.assertIn("risk management", profile["affected_qms_topics"])

    def test_reference_hint_fallback_when_summary_empty(self) -> None:
        item = make_item("MDCG 2019-9", "MDCG 2023-3", summary="", hint="Official guidance says PMS plan expectations changed.")
        with tempfile.TemporaryDirectory() as tmpdir:
            profiles = build_amendment_profiles([item], output_dir=Path(tmpdir), regulation_files_dir=Path(tmpdir) / "missing")

        self.assertEqual(profiles[0]["source_mode"], "excel_reference_hint")
        self.assertEqual(profiles[0]["confidence"], "Low")
        self.assertTrue(profiles[0]["exact_amendment_known"])

    def test_no_evidence_profile_and_unresolved_csv(self) -> None:
        item = make_item("ISO 9999:2000", "ISO 9999:2020", summary="", hint="")
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir)
            profiles = build_amendment_profiles([item], output_dir=output, regulation_files_dir=Path(tmpdir) / "missing")
            unresolved = (output / "unresolved_amendments.csv").read_text(encoding="utf-8-sig")

        self.assertEqual(profiles[0]["source_mode"], "no_amendment_evidence")
        self.assertFalse(profiles[0]["exact_amendment_known"])
        self.assertIn("ISO 9999:2000", unresolved)

    def test_vague_summary_is_low_confidence(self) -> None:
        item = make_item("ISO 13485:2003", "ISO 13485:2016", summary="Updated.")
        with tempfile.TemporaryDirectory() as tmpdir:
            profiles = build_amendment_profiles([item], output_dir=Path(tmpdir), regulation_files_dir=Path(tmpdir) / "missing")

        self.assertEqual(profiles[0]["confidence"], "Low")

    def test_topic_and_keyword_extraction(self) -> None:
        topics, keywords = extract_qms_topics_and_keywords("新增UDI标签要求，并加强上市后PMS surveillance。")

        self.assertIn("labeling / UDI / IFU", topics)
        self.assertIn("post-market surveillance", topics)
        self.assertIn("UDI", keywords)

    def test_cache_invalidation_when_excel_summary_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "profiles"
            first = make_item("ISO 14971:2019", "ISO 14971:2025", summary="Updated risk requirements.")
            second = make_item("ISO 14971:2019", "ISO 14971:2025", summary="Updated clinical risk requirements.")
            first_profile = build_amendment_profiles([first], output_dir=output, regulation_files_dir=Path(tmpdir) / "missing")[0]
            second_profile = build_amendment_profiles([second], output_dir=output, regulation_files_dir=Path(tmpdir) / "missing")[0]

        self.assertNotEqual(first_profile["cache_key"], second_profile["cache_key"])
        self.assertEqual(second_profile["amendment_summary_used"], "Updated clinical risk requirements.")

    def test_file_comparison_mode_with_text_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            files = root / "regulation_files"
            files.mkdir()
            (files / "ISO 14971 2019 old.txt").write_text(
                "1 Scope\nThe organization shall document risk controls.\n2 Risk\nThe organization shall maintain risk records.",
                encoding="utf-8",
            )
            (files / "ISO 14971 2025 latest.txt").write_text(
                "1 Scope\nThe organization shall document risk controls and risk-benefit analysis.\n2 Risk\nThe organization shall maintain risk records.",
                encoding="utf-8",
            )
            item = make_item("ISO 14971:2019", "ISO 14971:2025", original_year="2019", latest_year="2025")

            profile = build_amendment_profiles([item], output_dir=root / "profiles", regulation_files_dir=files)[0]

            self.assertEqual(profile["source_mode"], "old_vs_latest_file_comparison")
            self.assertTrue(profile["full_text_available"])
            self.assertTrue(profile["exact_clause_verified"])
            self.assertTrue((root / "profiles" / "sources" / "ISO_14971" / "clause_comparison_results.csv").exists())

    def test_excel_mode_skips_file_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            files = root / "regulation_files"
            files.mkdir()
            (files / "ISO 14971 2019 old.txt").write_text("1 Scope\nOld requirement", encoding="utf-8")
            (files / "ISO 14971 2025 latest.txt").write_text("1 Scope\nLatest requirement", encoding="utf-8")
            item = make_item("ISO 14971:2019", "ISO 14971:2025", original_year="2019", latest_year="2025")

            profile = build_amendment_profiles(
                [item],
                output_dir=root / "profiles",
                regulation_files_dir=files,
                use_file_comparison=False,
            )[0]

        self.assertEqual(profile["source_mode"], "excel_amendment_summary")
        self.assertFalse(profile["exact_clause_verified"])

    def test_collect_writes_new_output_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            excel = root / "updates.xlsx"
            output = root / "regulation_amendment_profiles"
            pd.DataFrame(
                [
                    {
                        "原标准编号": "ISO 13485:2003",
                        "原版本年份": 2003,
                        "最新版本": "ISO 13485:2016",
                        "最新年份": 2016,
                        "状态": "已更新",
                        "主要变更内容": "Updated document and record control requirements.",
                        "查询参考": "",
                    }
                ]
            ).to_excel(excel, index=False, sheet_name="原始总表")

            summary = collect_regulation_amendments(excel, output_dir=output, max_items=1)

            item_dir = output / "sources" / "ISO_13485"
            self.assertEqual(summary["total_updated_rows_processed"], 1)
            self.assertEqual(summary["excel_amendment_summary"], 1)
            self.assertTrue((item_dir / "amendment_profile_row_2.json").exists())
            self.assertTrue((output / "amendment_profiles_master.csv").exists())
            self.assertTrue((output / "amendment_profiles_master.json").exists())
            self.assertTrue((output / "unresolved_amendments.csv").exists())
            payload = json.loads((item_dir / "amendment_profile_row_2.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["source_mode"], "excel_amendment_summary")


def make_item(
    original: str,
    latest: str,
    summary: str = "Updated risk requirements.",
    hint: str = "",
    original_year: str = "",
    latest_year: str = "",
):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "updates.xlsx"
        pd.DataFrame(
            [
                {
                    "原标准编号": original,
                    "原版本年份": original_year,
                    "最新版本": latest,
                    "最新年份": latest_year,
                    "状态": "已更新",
                    "主要变更内容": summary,
                    "查询参考": hint,
                }
            ]
        ).to_excel(path, index=False, sheet_name="原始总表")
        return load_excel_items(path, "原始总表")[0]


if __name__ == "__main__":
    unittest.main()
