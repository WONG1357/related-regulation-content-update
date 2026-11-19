"""Exercise the actual Streamlit workflow with synthetic local catalogue data.

AppTest does not supply file-upload bytes. Only that UI boundary and the network
catalogue fetch are substituted; query validation, storage, comparison and report
generation execute their real implementations.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from modules.standards_catalogue import CatalogueFetchError, load_iso_catalogue
from modules.standards_update_workflow import report_json, report_markdown


class _Upload(io.BytesIO):
    def __init__(self, name: str, content: bytes):
        super().__init__(content)
        self.name = name


@unittest.skipUnless(importlib.util.find_spec("streamlit"), "Streamlit is not installed")
class StandardsAppTests(unittest.TestCase):
    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self) -> None:
        from streamlit.delta_generator import DeltaGenerator
        from streamlit.testing.v1 import AppTest

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        base = {
            "deliverableType": "IS", "supplementType": None,
            "title": {"en": "Synthetic test publication; not a real standard"},
            "icsCode": ["11.040.01"], "ownerCommittee": "ISO/TC 999",
            "scope": {"en": "Synthetic medical testing fixture"},
        }
        records = [
            {**base, "id": 90001, "reference": "ISO 99999:2020", "publicationDate": "2020-06-01",
             "edition": 1, "currentStage": 9599, "replaces": [], "replacedBy": [90002]},
            {**base, "id": 90002, "reference": "ISO 99999:2026", "publicationDate": "2026-06-01",
             "edition": 2, "currentStage": 6060, "replaces": [90001], "replacedBy": []},
        ]
        path = self.root / "fixture.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
        self.snapshot = load_iso_catalogue(path, source_url="synthetic AppTest fixture",
                                           retrieved_at="2026-11-19T10:00:00+08:00")
        self.fetch = self.start_patch(patch("modules.standards_catalogue.fetch_iso_catalogue", return_value=self.snapshot))
        self.start_patch(patch.dict(os.environ, {"REGULATORY_APP_DATA_DIR": str(self.root / "data")}))
        self.start_patch(patch("modules.standards_query.hong_kong_today", return_value=date(2026, 11, 19)))

        original = DeltaGenerator.file_uploader

        def file_uploader(generator, label, *args, **kwargs):
            original(generator, label, *args, **kwargs)
            if label.startswith("舊版："):
                return _Upload("old.txt", b"1 Scope\nThe manufacturer shall maintain a tolerance of < 5 mm during testing.")
            if label.startswith("新版："):
                return _Upload("new.txt", b"1 Scope\nThe manufacturer shall maintain a tolerance of > 5 mm during testing.")
            return None

        self.start_patch(patch.object(DeltaGenerator, "file_uploader", new=file_uploader))
        app_file = Path(__file__).resolve().parents[1] / "standards_updates_app.py"
        self.app = AppTest.from_file(str(app_file), default_timeout=15).run()
        self.assertEqual(len(self.app.exception), 0)

    def submit_query(self) -> None:
        next(field for field in self.app.text_area if field.label.startswith("標準編號或 ICS")).set_value("ISO 99999")
        next(button for button in self.app.button if button.label == "查找更新").click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(len(self.app.error), 0)

    def test_query_compare_and_export_keep_evidence_and_provenance(self) -> None:
        self.submit_query()
        self.assertEqual(self.app.dataframe[0].value["標準"].tolist(), ["ISO 99999:2026"])
        self.assertEqual(next(s for s in self.app.selectbox if s.label == "選擇發布記錄").value, 90002)
        self.assertEqual(next(s for s in self.app.selectbox if s.label == "選擇官方列出的上一版本").value, 90001)
        compare = next(button for button in self.app.button if button.label == "產生差異證據")
        self.assertFalse(compare.disabled)
        compare.click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(len(self.app.error), 0)
        report = self.app.session_state["standards_report"]
        comparison = report["comparisons"][0]
        self.assertEqual(comparison["status"], "review_required")
        self.assertEqual(comparison["changes"][0]["critical_tokens_changed"], ["operator"])
        self.assertEqual(comparison["catalogue_link"]["snapshot_sha256"], self.snapshot.sha256)
        self.assertIn("< 5 mm", comparison["changes"][0]["old_text"])
        self.assertIn("> 5 mm", comparison["changes"][0]["new_text"])
        self.assertEqual(report["trace"][-1]["step"], "compare_selected_files")
        self.assertEqual(json.loads(report_json(report))["comparisons"][0]["changes"], comparison["changes"])
        markdown = report_markdown(report)
        self.assertIn(comparison["changes"][0]["id"], markdown)
        self.assertIn("ISO 99999:2020", markdown)
        self.assertIn("ISO 99999:2026", markdown)
        self.assertIn("ODC Attribution", markdown)
        self.assertEqual({item.proto.label for item in self.app.get("download_button")},
                         {"下載完整 JSON", "下載 Markdown 報告"})
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(len(list((self.root / "data" / "standards" / "documents").glob("*.txt"))), 2)

    def test_invalid_new_query_clears_old_results_and_does_not_refetch(self) -> None:
        self.submit_query()
        self.app.date_input[0].set_value(date(2026, 11, 19))
        self.app.date_input[1].set_value(date(2026, 1, 1))
        next(button for button in self.app.button if button.label == "查找更新").click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertTrue(any("start_date" in error.value for error in self.app.error))
        self.assertEqual(len(self.app.dataframe), 0)
        self.assertEqual(len(self.app.get("download_button")), 0)
        self.assertEqual(self.fetch.call_count, 1)

    def test_failed_refresh_is_visible_and_does_not_substitute_cached_snapshot(self) -> None:
        self.submit_query()
        old_source_hash = self.app.session_state["standards_report"]["source"]["sha256"]
        self.fetch.side_effect = CatalogueFetchError("Synthetic network failure")
        next(button for button in self.app.button if button.label == "更新／載入目錄").click().run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertTrue(any("Synthetic network failure" in error.value for error in self.app.error))
        self.assertNotIn("standards_snapshot", self.app.session_state)
        self.assertFalse(any(button.label == "產生差異證據" for button in self.app.button))
        # A previously completed report may remain exportable, but its source
        # hash must remain the old one rather than claiming a fresh acquisition.
        if "standards_report" in self.app.session_state:
            self.assertEqual(self.app.session_state["standards_report"]["source"]["sha256"], old_source_hash)
        self.assertEqual(self.fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
