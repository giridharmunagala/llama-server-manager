import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from file_picker import choose_gguf, desktop_dialog, select_model_file, tk_dialog
from manager import ManagerError


class FilePickerTests(unittest.TestCase):
    def test_selected_file_is_absolute_and_cancel_returns_none(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model with spaces.GGUF"
            model.write_bytes(b"GGUF")
            with patch("file_picker.desktop_dialog", return_value=str(model)):
                self.assertEqual(choose_gguf(), str(model.resolve()))
            with patch("file_picker.desktop_dialog", return_value=None):
                self.assertIsNone(choose_gguf())
            for invalid in (directory, str(model.with_suffix(".txt"))):
                with patch("file_picker.desktop_dialog", return_value=invalid):
                    with self.assertRaisesRegex(ManagerError, "existing .gguf file"):
                        choose_gguf()

    def test_linux_desktop_pickers(self):
        for tool in ("zenity", "kdialog"):
            with self.subTest(tool=tool), patch("file_picker.sys.platform", "linux"), \
                    patch("file_picker.shutil.which",
                          side_effect=lambda name: f"/usr/bin/{name}" if name == tool else None), \
                    patch("file_picker.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "/models/a b.gguf\n", "")
                self.assertEqual(desktop_dialog(), "/models/a b.gguf")
                self.assertEqual(run.call_args.args[0][0], f"/usr/bin/{tool}")
                run.return_value = subprocess.CompletedProcess([], 1, "", "")
                self.assertIsNone(desktop_dialog())
                run.return_value = subprocess.CompletedProcess([], 2, "", "No display")
                with self.assertRaisesRegex(ManagerError, "No display"):
                    desktop_dialog()
                run.return_value = subprocess.CompletedProcess([], 1, "", "Cannot open display")
                with self.assertRaisesRegex(ManagerError, "Cannot open display"):
                    desktop_dialog()

    def test_windows_and_linux_without_desktop_tools_use_tk(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), patch("file_picker.sys.platform", platform), \
                    patch("file_picker.shutil.which", return_value=None), \
                    patch("file_picker.tk_dialog", return_value="C:/models/test.gguf") as dialog:
                self.assertEqual(desktop_dialog(), "C:/models/test.gguf")
                dialog.assert_called_once_with()

    def test_tk_dialog_filters_files_and_cleans_up(self):
        tk = MagicMock()
        tk.TclError = RuntimeError
        tk.filedialog.askopenfilename.return_value = "/models/test.gguf"
        with patch.dict(sys.modules, {"tkinter": tk, "tkinter.filedialog": tk.filedialog}):
            self.assertEqual(tk_dialog(), "/models/test.gguf")
            self.assertEqual(tk.filedialog.askopenfilename.call_args.kwargs["filetypes"],
                             [("GGUF model files", "*.gguf")])
            tk.Tk.return_value.destroy.assert_called_once()

    def test_missing_tk_and_display_report_actionable_errors(self):
        with patch.dict(sys.modules, {"tkinter": None}):
            with self.assertRaisesRegex(ManagerError, "python3-tk"):
                tk_dialog()
        tk = MagicMock()
        tk.TclError = RuntimeError
        tk.Tk.side_effect = RuntimeError("No display")
        with patch.dict(sys.modules, {"tkinter": tk, "tkinter.filedialog": tk.filedialog}):
            with self.assertRaisesRegex(ManagerError, "desktop session"):
                tk_dialog()

    def test_subprocess_protocol_and_errors(self):
        with patch("file_picker.subprocess.run") as run:
            for selected in ("/models/a b.gguf", None):
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"path": selected}), "")
                self.assertEqual(select_model_file(), selected)
                self.assertEqual(run.call_args.args[0][0], sys.executable)
            run.return_value = subprocess.CompletedProcess([], 1, '{"error":"No display"}', "")
            with self.assertRaisesRegex(ManagerError, "No display"):
                select_model_file()
            for response in ("", "[]", "{}"):
                run.return_value = subprocess.CompletedProcess([], 0, response, "Picker failed")
                with self.assertRaises(ManagerError):
                    select_model_file()
