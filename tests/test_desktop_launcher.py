from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_launcher


class DesktopLauncherTests(unittest.TestCase):
    def test_prepare_runtime_environment_creates_private_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "REGULATORY_DESKTOP_HOME": str(root / "state"),
                    "REGULATORY_DESKTOP_DOCUMENTS": str(root / "documents"),
                },
                clear=True,
            ):
                paths = desktop_launcher.prepare_runtime_environment()
                self.assertEqual(paths["data"], root / "state" / "data")
                self.assertEqual(paths["config"], root / "state" / "config")
                self.assertEqual(paths["original_files"], root / "documents" / "original file")
                for path in paths.values():
                    self.assertTrue(path.is_dir())
                self.assertEqual(os.environ["REGULATORY_APP_DATA_DIR"], str(paths["data"]))
                self.assertEqual(os.environ["REGULATORY_APP_CONFIG_DIR"], str(paths["config"]))
                self.assertEqual(os.environ["REGULATORY_ORIGINAL_FILE_DIR"], str(paths["original_files"]))

    def test_choose_port_returns_a_bindable_local_port(self) -> None:
        candidate = MagicMock()
        candidate.__enter__.return_value = candidate
        candidate.getsockname.return_value = ("127.0.0.1", 49152)
        with patch("desktop_launcher.socket.socket", return_value=candidate):
            port = desktop_launcher.choose_port(0)
        self.assertEqual(port, 49152)
        candidate.bind.assert_called_once_with(("127.0.0.1", 0))

    def test_source_child_command_relaunches_the_launcher(self) -> None:
        with patch.object(sys, "frozen", False, create=True):
            command = desktop_launcher.child_command(9876)
        self.assertEqual(command[-2:], ["--streamlit-child", "9876"])
        self.assertEqual(Path(command[1]).name, "desktop_launcher.py")


if __name__ == "__main__":
    unittest.main()
