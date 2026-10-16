"""Open a desktop file dialog in a separate process, on its GUI main thread."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

from manager import ManagerError


def desktop_dialog():
    if sys.platform.startswith("linux"):
        zenity = shutil.which("zenity")
        kdialog = shutil.which("kdialog")
        if zenity:
            command = [zenity, "--file-selection",
                       "--title=Select a GGUF model (first part for split models)",
                       "--file-filter=GGUF model files | *.gguf *.GGUF"]
        elif kdialog:
            command = [kdialog, "--title", "Select a GGUF model (first part for split models)",
                       "--getopenfilename", str(Path.home()), "*.gguf *.GGUF|GGUF model files"]
        else:
            command = None
        if command:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            if result.returncode == 1 and not result.stderr.strip():
                return None
            if result.returncode != 0:
                raise ManagerError(f"Cannot open the file picker: {result.stderr.strip()}", 500)
            return result.stdout.rstrip("\r\n") or None
    return tk_dialog()


def tk_dialog():
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise ManagerError(
            "File browsing requires Tk support for Python. On Linux, install "
            "python3-tk (Debian/Ubuntu) or python3-tkinter (Fedora). On Windows, "
            "enable Tcl/Tk in the Python installer. You can still paste the file path."
        ) from exc
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            parent=root, title="Select a GGUF model (first part for split models)",
            filetypes=[("GGUF model files", "*.gguf")])
    except tk.TclError as exc:
        raise ManagerError(
            f"Cannot open the file picker. Run the dashboard in a desktop session "
            f"or paste the file path: {exc}"
        ) from exc
    finally:
        if root is not None:
            root.destroy()
    return selected or None


def choose_gguf():
    selected = desktop_dialog()
    if selected is None:
        return None
    path = Path(selected).resolve()
    if not path.is_file() or path.suffix.lower() != ".gguf":
        raise ManagerError("Select an existing .gguf file, not a folder.")
    return str(path)


def select_model_file():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        capture_output=True, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise ManagerError(
            f"Cannot open the file picker: {result.stderr.strip() or 'invalid picker response'}", 500
        ) from exc
    if not isinstance(value, dict):
        raise ManagerError("Invalid file picker response.", 500)
    if result.returncode:
        raise ManagerError(value.get("error", "Cannot open the file picker."), 500)
    if "path" not in value or (value["path"] is not None and not isinstance(value["path"], str)):
        raise ManagerError("Invalid file picker path.", 500)
    return value["path"]


if __name__ == "__main__":
    try:
        print(json.dumps({"path": choose_gguf()}))
    except (ManagerError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
