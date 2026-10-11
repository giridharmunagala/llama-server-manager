"""Launch and monitor one locally owned llama.cpp server."""

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading


DEFAULT_EXECUTABLE = "llama"
# llama.cpp now ships one `llama` binary (`llama serve`); `llama-server` is the legacy name.
AUTO_COMMANDS = ("llama", "llama-server")


class ManagerError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_command_name(value):
    """A bare program name such as ``llama`` (looked up on PATH)."""
    value = value.strip()
    return bool(value) and not any(char in value for char in "/\\:") and value not in (".", "..")


def needs_serve_subcommand(executable):
    """The unified ``llama`` binary needs ``serve``; ``llama-server`` does not."""
    return Path(str(executable)).stem.lower() == "llama"


def _search_path():
    entries = os.environ.get("PATH", "").split(os.pathsep)
    if os.name == "nt":
        # The launcher may predate a fresh install; read the persisted PATH too.
        import winreg
        for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, key) as handle:
                    entries += os.path.expandvars(winreg.QueryValueEx(handle, "Path")[0]).split(os.pathsep)
            except OSError:
                pass
    return os.pathsep.join(dict.fromkeys(entry for entry in entries if entry))


def _common_locations(name):
    home = Path.home()
    if os.name == "nt":
        exe = name if name.lower().endswith(".exe") else name + ".exe"
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates = [local / "Microsoft" / "WindowsApps" / exe,
                      local / "Microsoft" / "WinGet" / "Links" / exe]
        packages = local / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            for package in sorted(packages.glob("ggml.llamacpp*")):
                candidates += [package / exe, *sorted(package.glob(f"*/{exe}"))]
        candidates += [home / "scoop" / "shims" / exe,
                       Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "chocolatey" / "bin" / exe,
                       home / "llama.cpp" / "build" / "bin" / "Release" / exe]
    else:
        candidates = [Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name,
                      Path("/usr/bin") / name, home / ".local" / "bin" / name,
                      home / "llama.cpp" / "build" / "bin" / name]
    return candidates


def _find_one(name):
    found = shutil.which(name, path=_search_path())
    if found:
        return Path(found).resolve()
    for candidate in _common_locations(name):
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return candidate.resolve()
    return None


def find_executable(name=DEFAULT_EXECUTABLE):
    """Resolve a command name from PATH or well-known install locations.

    The automatic names (``llama`` or legacy ``llama-server``) try the unified
    ``llama`` binary first and fall back to ``llama-server``.
    """
    names = AUTO_COMMANDS if name.lower() in AUTO_COMMANDS else (name,)
    for candidate in names:
        found = _find_one(candidate)
        if found is not None:
            return found
    return None


@dataclass(frozen=True)
class LaunchConfig:
    executable: str = DEFAULT_EXECUTABLE
    model: str = ""
    port: int = 8080
    context_size: int = 4096
    threads: int = 0
    gpu_layers: int = 0


def parse_config(value):
    if not isinstance(value, dict):
        raise ManagerError("Configuration must be a JSON object.")
    fields = set(LaunchConfig.__dataclass_fields__)
    if set(value) != fields:
        raise ManagerError("Configuration must contain exactly: " + ", ".join(sorted(fields)))
    for key in ("executable", "model"):
        if not isinstance(value[key], str) or not value[key].strip() or "\0" in value[key]:
            raise ManagerError(f"{key} must be a nonempty file path without null characters.")
    limits = {"port": (1, 65535), "context_size": (1, 1048576),
              "threads": (0, 1024), "gpu_layers": (0, 999)}
    for key, (low, high) in limits.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ManagerError(f"{key} must be an integer between {low} and {high}.")
    return LaunchConfig(**value)


class StrictRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        # Logging normally swallows write errors; surface failures in the dashboard.
        raise


class ServerManager:
    def __init__(self, data_dir, base_dir):
        self.data_dir = Path(data_dir)
        self.base_dir = Path(base_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._actions = threading.Lock()
        self._process = None
        self._job = None
        self._process_group = None
        self._workers = []
        self._stop_event = threading.Event()
        self._closed = False
        self._released = False
        self._state = "stopped"
        self._error = None
        self._exit_code = None
        self._started_at = None
        self._health_detail = "Server is stopped."
        self._inflight = 0
        self._lines = deque(maxlen=500)
        self._instance_file = (self.data_dir / "manager.lock").open("a+b")
        try:
            self._acquire_instance_lock()
            config_path = self.data_dir / "config.json"
            self.config = (parse_config(json.loads(config_path.read_text(encoding="utf-8")))
                           if config_path.exists() else LaunchConfig())
            log_path = self.data_dir / "llama-server.log"
            if log_path.exists():
                with log_path.open("rb") as stream:
                    stream.seek(max(0, log_path.stat().st_size - 131072))
                    self._lines.extend(stream.read().decode("utf-8", errors="replace").splitlines()[-500:])
            self._log_handler = StrictRotatingHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
            self._log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            from catalog import Catalog
            from ledger import Ledger
            from observability import Observability
            self.catalog = Catalog(self.data_dir)
            workspace_path = self.data_dir / "workspace.json"
            self.workspace = (self._parse_workspace(json.loads(workspace_path.read_text(encoding="utf-8")))
                              if workspace_path.exists() else
                              {"mode": "single", "executable": self.config.executable,
                               "port": self.config.port})
            self.ledger = Ledger(self.data_dir / "statistics.sqlite3")
            self.observability = Observability(self.data_dir / "observability.sqlite3")
        except (OSError, ValueError, ManagerError, sqlite3.Error):
            self._instance_file.close()
            raise

    def _acquire_instance_lock(self):
        try:
            if os.name == "nt":
                import msvcrt
                self._instance_file.seek(0, os.SEEK_END)
                if self._instance_file.tell() == 0:
                    self._instance_file.write(b"\0")
                    self._instance_file.flush()
                self._instance_file.seek(0)
                msvcrt.locking(self._instance_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._instance_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ManagerError(
                "Cannot lock local data; another dashboard may already be using this data directory.",
                409) from exc

    def _log(self, message):
        with self._lock:
            self._lines.append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
            record = logging.LogRecord("llama-server", logging.INFO, "", 0, message, (), None)
            try:
                self._log_handler.emit(record)
            except OSError as exc:
                self._error = f"Cannot write server logs: {exc}"
                print(self._error, file=sys.stderr)

    def _resolve_executable(self, value):
        raw = value.strip()
        path = Path(raw).expanduser()
        if is_command_name(raw) and not (self.base_dir / path).is_file():
            found = find_executable(raw)
            if found is None:
                install = ("winget install llama.cpp" if os.name == "nt"
                           else "brew install llama.cpp (or your package manager)")
                raise ManagerError(
                    "llama.cpp was not found: neither 'llama' nor 'llama-server' is on PATH or in common "
                    f"install locations. Install llama.cpp (for example: {install}) or set the full path "
                    "in Router setup." if raw.lower() in AUTO_COMMANDS else
                    f"'{raw}' was not found on PATH or in common install locations. "
                    f"Install llama.cpp (for example: {install}) or set the full path in Router setup.")
            return found
        if not path.is_absolute():
            path = self.base_dir / path
        return path.resolve()

    def _validate_paths(self, config, router=False):
        paths = {}
        for key in (("executable",) if router else ("executable", "model")):
            if key == "executable":
                path = self._resolve_executable(config.executable)
            else:
                path = Path(getattr(config, key).strip()).expanduser()
                if not path.is_absolute():
                    path = self.base_dir / path
                path = path.resolve()
            if not path.is_file():
                raise ManagerError(f"{key} file does not exist: {path}")
            paths[key] = path
        if os.name == "nt":
            if paths["executable"].suffix.lower() != ".exe":
                raise ManagerError("On Windows, choose the installed llama.exe (or llama-server.exe), not a batch file.")
        elif not os.access(paths["executable"], os.X_OK):
            raise ManagerError(f"Executable is not executable; check its permissions: {paths['executable']}")
        if router:
            return LaunchConfig(**{**asdict(config), "executable": str(paths["executable"])})
        if paths["model"].suffix.lower() != ".gguf":
            raise ManagerError("Model must be an existing local .gguf file.")
        try:
            with paths["model"].open("rb") as stream:
                if stream.read(4) != b"GGUF":
                    raise ManagerError("Model does not have a GGUF header; choose a valid local GGUF model.")
        except OSError as exc:
            raise ManagerError(f"Cannot read model: {exc}") from exc
        return LaunchConfig(**{**asdict(config), **{key: str(path) for key, path in paths.items()}})

    def _check_idle(self):
        if self._closed:
            raise ManagerError("Dashboard is shutting down.", 409)
        self._refresh_exit()
        if self._process is not None:
            raise ManagerError("Stop the managed server before changing settings or starting again.", 409)

    def save_config(self, value):
        config = self._validate_paths(parse_config(value))
        with self._actions, self._lock:
            self._check_idle()
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=self.data_dir, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(asdict(config), stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.data_dir / "config.json")
            except OSError as exc:
                raise ManagerError(f"Cannot save configuration: {exc}", 500) from exc
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            self.config = config
            return asdict(config)

    def command(self, config):
        launcher = [config.executable, *(["serve"] if needs_serve_subcommand(config.executable) else [])]
        if self.workspace["mode"] == "router":
            return [*launcher, "--host", "127.0.0.1", "--port", str(config.port),
                    "--models-preset", str(self.catalog.active_path),
                    "--models-max", str(self.catalog.value["max_loaded"]),
                    "--models-autoload" if self.catalog.value["autoload"] else "--no-models-autoload"]
        command = [*launcher, "--model", config.model, "--host", "127.0.0.1",
                   "--port", str(config.port), "--ctx-size", str(config.context_size),
                   "--n-gpu-layers", str(config.gpu_layers)]
        if config.threads:
            command.extend(["--threads", str(config.threads)])
        return command

    @staticmethod
    def _parse_workspace(value):
        if not isinstance(value, dict) or set(value) != {"mode", "executable", "port"}:
            raise ManagerError("Workspace must contain mode, executable and port.")
        if value["mode"] not in ("single", "router"):
            raise ManagerError("Mode must be single or router.")
        parse_config({**asdict(LaunchConfig()), **{key: value[key] for key in ("executable", "port")},
                      "model": "router"})
        return dict(value)

    def detect_executable(self):
        with self._lock:
            configured = self.workspace["executable"] if self.workspace["mode"] == "router" else self.config.executable
        try:
            found = self._resolve_executable(configured or DEFAULT_EXECUTABLE)
            resolved = str(found) if found.is_file() else None
        except ManagerError:
            resolved = None
        return {"configured": configured, "automatic": is_command_name(configured or DEFAULT_EXECUTABLE),
                "resolved": resolved,
                "command": (f"{Path(resolved).stem} serve" if needs_serve_subcommand(resolved)
                            else Path(resolved).stem) if resolved else None}

    def get_workspace(self):
        with self._lock:
            settings = dict(self.workspace)
            if settings["mode"] == "single":
                settings.update(executable=self.config.executable, port=self.config.port)
            return {"settings": settings, "catalog": self.catalog.value,
                    "pending_apply": getattr(self, "_pending_apply", False)}

    def save_workspace(self, value):
        from catalog import atomic_text
        clean = self._parse_workspace(value)
        clean["executable"] = clean["executable"].strip()
        config = LaunchConfig(**{**asdict(self.config), "executable": clean["executable"],
                                 "port": clean["port"]})
        if is_command_name(clean["executable"]):
            # Command names stay unresolved so PATH changes (upgrades) are picked up at start.
            validated = config
        else:
            validated = self._validate_paths(config, router=clean["mode"] == "router")
            clean["executable"] = validated.executable
        with self._actions, self._lock:
            self._check_idle()
            if clean["mode"] == "single":
                if validated.executable != self.config.executable or validated.port != self.config.port:
                    raise ManagerError("Save single-model launch settings before switching to single mode.")
            atomic_text(self.data_dir / "workspace.json", json.dumps(clean, indent=2) + "\n")
            self.workspace = clean
        return self.get_workspace()

    def save_catalog(self, value):
        with self._actions, self._lock:
            if self._closed:
                raise ManagerError("Dashboard is shutting down.", 409)
            if self._process is not None and isinstance(value, dict) and any(
                    value.get(key) != self.catalog.value[key] for key in ("max_loaded", "autoload")):
                raise ManagerError("Stop the router before changing capacity or autoload.", 409)
            self.catalog.save(value)
            self._pending_apply = self._process is not None
            return self.get_workspace()

    def import_catalog(self, source):
        with self._actions, self._lock:
            if self._closed:
                raise ManagerError("Dashboard is shutting down.", 409)
            self.catalog.import_file(source)
            self._pending_apply = self._process is not None
            return self.get_workspace()

    def _effective_config(self):
        if self.workspace["mode"] == "router":
            return LaunchConfig(**{**asdict(self.config), "executable": self.workspace["executable"],
                                   "port": self.workspace["port"]})
        return self.config

    def _router_request(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.workspace["port"], timeout=10)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if response.status != 200:
                raise ManagerError(f"Router {path} returned HTTP {response.status}. See server logs.", 502)
            if len(raw) > 1024 * 1024:
                raise ManagerError("Router response is too large.", 502)
            return json.loads(raw)
        except (OSError, HTTPException, ValueError) as exc:
            raise ManagerError(f"Cannot communicate with llama router: {exc}", 502) from exc
        finally:
            connection.close()

    def router_models(self):
        with self._lock:
            if self.workspace["mode"] != "router" or self._process is None:
                return {"data": []}
            value = self._router_request("GET", "/models")
            if not isinstance(value, dict) or not isinstance(value.get("data"), list):
                raise ManagerError("Unexpected router /models response.", 502)
            return value

    def router_action(self, action, model=None):
        with self._actions, self._lock:
            self._refresh_exit()
            if self.workspace["mode"] != "router" or self._process is None:
                raise ManagerError("Start the router first.", 409)
            if self._inflight:
                raise ManagerError("Wait for tracked requests to finish before changing loaded models.", 409)
            if action == "apply":
                self.catalog.write_active()
                result = self._router_request("GET", "/models?reload=1")
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise ManagerError("Unexpected router reload response.", 502)
                self._pending_apply = False
                self._log("Applied router presets; changed models may have been unloaded.")
                return result
            if action not in {"load", "unload"} or model not in self.catalog.value["enabled"]:
                raise ManagerError("Select an enabled model and a load/unload action.")
            if getattr(self, "_pending_apply", False):
                raise ManagerError("Apply saved presets before loading or unloading models.", 409)
            result = self._router_request("POST", f"/models/{action}", {"model": model})
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ManagerError(f"Router did not confirm {action}.", 502)
            self._log(f"Router {action} requested for {model}.")
            return result

    def begin_generation(self, model):
        with self._lock:
            self._refresh_exit()
            if self._process is None or self._state != "running":
                raise ManagerError("Managed server is not ready.", 503)
            router = self.workspace["mode"] == "router"
            if router and (getattr(self, "_pending_apply", False)
                           or model not in self.catalog.value["enabled"]):
                raise ManagerError("Model is not enabled, or saved presets need applying.", 409)
            identity = model if router else self.config.model
            configuration = ({"mode": "router", "model": model,
                              "executable": self.workspace["executable"],
                              "options": {**self.catalog.value["defaults"],
                                          **self.catalog.value["models"][model]}}
                             if router else {"mode": "single", **asdict(self.config)})
            request_id = self.ledger.begin_request(identity, configuration)
            self._inflight += 1
            return request_id, self._effective_config().port, None

    def end_generation(self):
        with self._lock:
            self._inflight -= 1

    def get_config(self):
        with self._lock:
            return asdict(self.config)

    @staticmethod
    def _check_port(port):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                if os.name == "nt":
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise ManagerError(f"Server port {port} is occupied or unavailable: {exc}", 409) from exc

    def start(self):
        with self._actions, self._lock:
            self._check_idle()
            router = self.workspace["mode"] == "router"
            config = self._validate_paths(self._effective_config(), router=router)
            if not router:
                parse_config(asdict(config))
            else:
                self.catalog.write_active()
            self._check_port(config.port)
            self._error = None
            self._exit_code = None
            self._log("Starting llama.cpp server.")
            self.ledger.start_session(self.workspace["mode"])
            process = None
            try:
                if router and os.name == "nt":
                    from windows_job import OwnedJob
                    self._job = OwnedJob()
                process = subprocess.Popen(
                    self.command(config), cwd=str(Path(config.executable).parent),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    bufsize=1, shell=False,
                    creationflags=0x00000004 if router and os.name == "nt" else 0,
                    start_new_session=router and os.name != "nt")
                if self._job is not None:
                    self._job.attach_and_resume(process)
            except OSError as exc:
                if process is not None:
                    process.kill()
                    process.wait(timeout=5)
                    if process.stdout is not None:
                        process.stdout.close()
                if self._job is not None:
                    self._job.close()
                    self._job = None
                self.ledger.end_session("launch_failed")
                self._state = "error"
                self._error = f"Could not launch llama.cpp: {exc}"
                self._log(self._error)
                raise ManagerError(self._error, 500) from exc
            self._process = process
            self._process_group = process.pid if router and os.name != "nt" else None
            self._pending_apply = False
            self._state = "starting"
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._health_detail = "Waiting for /health; the model may still be loading."
            self._stop_event = threading.Event()
            new_workers = [
                threading.Thread(target=self._read_output, args=(process,), daemon=True),
                threading.Thread(target=self._monitor, args=(process, config.port, self._stop_event),
                                 daemon=True),
            ]
            self._workers = [worker for worker in self._workers if worker.is_alive()] + new_workers
            for worker in new_workers:
                worker.start()
            return self.status()

    def _read_output(self, process):
        from observability import LogObserver
        observer = LogObserver(self.observability,
                               self.config.model if self.workspace["mode"] == "single" else None)
        collecting = True
        try:
            with process.stdout:
                while True:
                    line = process.stdout.readline(8192)
                    if not line:
                        break
                    self._log(line.rstrip("\r\n"))
                    if collecting:
                        try:
                            observer.feed(line.rstrip("\r\n"))
                        except sqlite3.Error as exc:
                            collecting = False
                            with self._lock:
                                self._error = f"Cannot persist observability; collection stopped for this run: {exc}"
                                self._log(self._error)
        except OSError as exc:
            with self._lock:
                self._error = f"Cannot read server output: {exc}"
                self._log(self._error)
        finally:
            try:
                observer.close()
            except sqlite3.Error as exc:
                with self._lock:
                    self._error = f"Cannot finalize observability: {exc}"
                    self._log(self._error)

    @staticmethod
    def _health(port):
        connection = HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            body = response.read(4096)
            if response.status == 200:
                try:
                    if json.loads(body).get("status") == "ok":
                        return True, "Ready: /health reports ok."
                except (ValueError, AttributeError):
                    return False, "Unexpected /health response; inspect server logs."
            return False, f"/health returned HTTP {response.status}; loading or unavailable."
        except (OSError, HTTPException) as exc:
            return False, f"Waiting for /health: {exc}"
        finally:
            connection.close()

    def _monitor(self, process, port, stop_event):
        while not stop_event.is_set():
            with self._lock:
                self._refresh_exit()
                if self._process is not process:
                    return
                try:
                    self.ledger.tick()
                except sqlite3.Error as exc:
                    self._error = f"Cannot persist lifecycle statistics: {exc}"
                    self._log(self._error)
            ready, detail = self._health(port)
            with self._lock:
                if self._process is not process or self._state == "stopping":
                    return
                self._refresh_exit()
                if self._process is not process:
                    return
                self._state = "running" if ready else "starting"
                self._health_detail = detail
            stop_event.wait(0.5)

    def _refresh_exit(self):
        if self._process is None:
            return
        code = self._process.poll()
        if code is None:
            return
        expected = self._state == "stopping"
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._process_group is not None:
            try:
                os.killpg(self._process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._process_group = None
        self._process = None
        self._exit_code = code
        self._state = "stopped" if expected else "error"
        self._health_detail = "Server is stopped."
        if not expected:
            self._error = f"llama.cpp server exited unexpectedly (code {code}). See the logs."
            self._log(self._error)
        else:
            self._log(f"llama.cpp server stopped (code {code}).")
        self._stop_event.set()
        self.ledger.end_session("stopped" if expected else "error")

    def status(self):
        with self._lock:
            self._refresh_exit()
            config = self._effective_config()
            command = self.command(config)
            return {"state": self._state, "pid": self._process.pid if self._process else None,
                    "error": self._error, "exit_code": self._exit_code,
                    "started_at": self._started_at, "health_detail": self._health_detail,
                    "url": f"http://127.0.0.1:{config.port}", "mode": self.workspace["mode"],
                    "inflight": self._inflight, "command": command,
                    "pending_apply": getattr(self, "_pending_apply", False)}

    def logs(self):
        with self._lock:
            return list(self._lines)

    def stop(self):
        with self._actions:
            with self._lock:
                self._refresh_exit()
                process = self._process
                if process is None:
                    return self.status()
                self._state = "stopping"
                self._stop_event.set()
                workers = list(self._workers)
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._log("Server did not stop within 5 seconds; killing the owned process.")
                    process.kill()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                with self._lock:
                    self._refresh_exit()
                    if self._process is not None:
                        self._state = "error"
                        self._error = f"Could not stop managed PID {process.pid}: {exc}"
                        self._log(self._error)
                        raise ManagerError(self._error, 500) from exc
            for worker in workers:
                worker.join(timeout=2)
            return self.status()

    def close(self):
        with self._lock:
            if self._released:
                return
            self._closed = True
        self.stop()
        for worker in self._workers:
            worker.join(timeout=2)
        self._log_handler.close()
        self.ledger.close()
        self.observability.close()
        self._instance_file.close()
        self._released = True
