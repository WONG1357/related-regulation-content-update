from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from regulatory_update_app.modules.document_library import load_or_extract_documents


class DocumentLibraryTests(unittest.TestCase):
    def test_cache_reuses_unchanged_and_refreshes_changed_source(self) -> None:
        calls = {"count": 0}

        def extractor(path):
            calls["count"] += 1
            return {
                "file_path": str(path),
                "file_name": Path(path).name,
                "document_code": "QSP0001",
                "sections": [],
                "main_sections": [],
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "QSP0001.docx"
            source.write_bytes(b"version one")
            items = [{"source_path": str(source), "extracted_path": str(source)}]
            cache = root / "cache.json"

            first, _, first_stats = load_or_extract_documents(
                items, cache, "v1", extractor=extractor, refresh_references=False
            )
            second, _, second_stats = load_or_extract_documents(
                items, cache, "v1", extractor=extractor, refresh_references=False
            )
            source.write_bytes(b"version two")
            third, _, third_stats = load_or_extract_documents(
                items, cache, "v1", extractor=extractor, refresh_references=False
            )

        self.assertEqual(calls["count"], 2)
        self.assertEqual(first_stats, {"hits": 0, "misses": 1, "total": 1})
        self.assertEqual(second_stats, {"hits": 1, "misses": 0, "total": 1})
        self.assertEqual(third_stats, {"hits": 0, "misses": 1, "total": 1})
        self.assertEqual(first[0]["source_file_name"], source.name)
        self.assertEqual(second[0]["document_code"], "QSP0001")
        self.assertEqual(third[0]["document_code"], "QSP0001")

    def test_extractor_version_invalidates_cache(self) -> None:
        calls = {"count": 0}

        def extractor(path):
            calls["count"] += 1
            return {"file_path": str(path), "document_code": "QSP0001", "sections": [], "main_sections": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "QSP0001.docx"
            source.write_bytes(b"same")
            items = [{"source_path": str(source), "extracted_path": str(source)}]
            cache = root / "cache.json"
            load_or_extract_documents(items, cache, "v1", extractor=extractor, refresh_references=False)
            load_or_extract_documents(items, cache, "v2", extractor=extractor, refresh_references=False)

        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
