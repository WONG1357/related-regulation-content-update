from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.app_paths import APP_ROOT, data_root, environment_paths, original_file_root


class AppPathTests(unittest.TestCase):
    def test_source_defaults_are_preserved(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(data_root(), APP_ROOT / "data")
            self.assertEqual(original_file_root(), APP_ROOT.parent / "original file")
            self.assertEqual(environment_paths(), [APP_ROOT / ".env", APP_ROOT / ".env.txt"])

    def test_desktop_overrides_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "REGULATORY_APP_DATA_DIR": str(root / "data"),
                    "REGULATORY_ORIGINAL_FILE_DIR": str(root / "original file"),
                    "REGULATORY_APP_CONFIG_DIR": str(root / "config"),
                },
                clear=True,
            ):
                self.assertEqual(data_root(), root / "data")
                self.assertEqual(original_file_root(), root / "original file")
                self.assertEqual(
                    environment_paths(),
                    [
                        root / "config" / ".env",
                        root / "config" / ".env.txt",
                        APP_ROOT / ".env",
                        APP_ROOT / ".env.txt",
                    ],
                )


if __name__ == "__main__":
    unittest.main()
