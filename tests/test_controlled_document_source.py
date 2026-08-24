from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.controlled_document_source import (
    ControlledDocumentSourceError,
    describe_configured_controlled_documents,
    save_uploaded_controlled_documents,
)


class UploadedFileStub:
    def __init__(self, name: str, content: bytes) -> None:
        self.name = name
        self._content = content

    def getvalue(self) -> bytes:
        return self._content


class ControlledDocumentSourceTests(unittest.TestCase):
    def test_uploaded_directory_is_saved_by_content_identity(self) -> None:
        uploads = [
            UploadedFileStub("original file/QSP0001-A Procedure.docx", b"docx-one"),
            UploadedFileStub("original file/subfolder/QSP0002-B Procedure.doc", b"doc-two"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            first = save_uploaded_controlled_documents(uploads, tmpdir)
            second = save_uploaded_controlled_documents(reversed(uploads), tmpdir)
            directory = Path(first["directory"])

            self.assertEqual(first["source_id"], second["source_id"])
            self.assertEqual(first["file_count"], 2)
            self.assertEqual(first["source_mode"], "uploaded_directory")
            self.assertEqual((directory / "QSP0001-A Procedure.docx").read_bytes(), b"docx-one")
            self.assertEqual((directory / "QSP0002-B Procedure.doc").read_bytes(), b"doc-two")
            self.assertFalse((Path(tmpdir) / "original file").exists())

    def test_duplicate_basenames_from_subfolders_fail_closed(self) -> None:
        uploads = [
            UploadedFileStub("folder-a/QSP0001.docx", b"one"),
            UploadedFileStub("folder-b/QSP0001.docx", b"two"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ControlledDocumentSourceError, "duplicate filenames"):
                save_uploaded_controlled_documents(uploads, tmpdir)

    def test_empty_or_unsupported_upload_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ControlledDocumentSourceError, "no supported"):
                save_uploaded_controlled_documents([], tmpdir)
            with self.assertRaisesRegex(ControlledDocumentSourceError, "Unsupported"):
                save_uploaded_controlled_documents([UploadedFileStub("notes.txt", b"notes")], tmpdir)

    def test_configured_folder_must_exist_and_contain_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = root / "missing"
            with self.assertRaisesRegex(ControlledDocumentSourceError, "unavailable"):
                describe_configured_controlled_documents(missing)

            source = root / "original file"
            source.mkdir()
            (source / "readme.txt").write_text("ignored", encoding="utf-8")
            with self.assertRaisesRegex(ControlledDocumentSourceError, "no supported"):
                describe_configured_controlled_documents(source)

            (source / "QSP0001.docx").write_bytes(b"document")
            result = describe_configured_controlled_documents(source)
            self.assertEqual(result["file_count"], 1)
            self.assertEqual(result["source_mode"], "configured_server_folder")

            original_id = result["source_id"]
            (source / "QSP0001.docx").write_bytes(b"updated-document")
            self.assertNotEqual(describe_configured_controlled_documents(source)["source_id"], original_id)


if __name__ == "__main__":
    unittest.main()
