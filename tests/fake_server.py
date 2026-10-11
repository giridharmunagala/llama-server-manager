"""Tiny llama-server stand-in used only by lifecycle tests; not an inference engine."""

import argparse
import configparser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import signal
import sys
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--models-preset")
    parser.add_argument("--models-max", type=int, default=1)
    parser.add_argument("--models-autoload", action="store_true")
    parser.add_argument("--no-models-autoload", action="store_true")
    parser.add_argument("--api-key")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ctx-size")
    parser.add_argument("--n-gpu-layers")
    parser.add_argument("--threads")
    args = parser.parse_args()
    presets = {}
    loaded = set()

    def reload_presets():
        ini = configparser.ConfigParser(interpolation=None)
        ini.read_string("[metadata]\n" + Path(args.models_preset).read_text())
        updated = {name: {**dict(ini["*"]), **dict(ini[name])}
                   for name in ini.sections() if name not in {"*", "metadata"}}
        for name in list(loaded):
            if presets.get(name) != updated.get(name):
                loaded.remove(name)
        presets.clear()
        presets.update(updated)

    if args.models_preset:
        reload_presets()
    model_path = args.model or next(iter(presets.values()))["model"]
    options = json.loads(Path(model_path).read_bytes()[4:] or b"{}")
    if options.get("spawn_worker"):
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        print(f"owned worker PID {worker.pid}", flush=True)
    print("fake server stdout", flush=True)
    print("fake server stderr", file=sys.stderr, flush=True)
    if "exit_immediately" in options:
        print("deliberate startup failure", flush=True)
        return options["exit_immediately"]
    if options.get("ignore_terminate") and sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if options.get("spam"):
        for index in range(700):
            print(f"log {index}", flush=True)
    started = time.monotonic()
    task_id = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/models"):
                if "reload=1" in self.path:
                    reload_presets()
                return self.reply(200, {"data": [
                    {"id": name, "status": {"value": "loaded" if name in loaded else "unloaded"}}
                    for name in presets]})
            ready = time.monotonic() - started >= options.get("delay", 0)
            code = 200 if ready else 503
            body = json.dumps({"status": "ok" if ready else "loading model"}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def reply(self, code, value):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            nonlocal task_id
            if args.api_key and self.headers.get("Authorization") != f"Bearer {args.api_key}":
                return self.reply(401, {"error": "Unauthorized"})
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            name = value.get("model")
            if args.models_preset:
                if name not in presets:
                    return self.reply(404, {"error": "Unknown model"})
                if self.path == "/models/unload":
                    loaded.discard(name)
                    return self.reply(200, {"success": True})
                if self.path == "/models/load" or not args.no_models_autoload:
                    if args.models_max and len(loaded) >= args.models_max and name not in loaded:
                        loaded.clear()
                    loaded.add(name)
                if self.path == "/models/load":
                    return self.reply(200, {"success": True})
                if name not in loaded:
                    return self.reply(409, {"error": "Model not loaded"})
            if value.get("test_case") == "error":
                return self.reply(500, {"error": "deliberate inference failure"})
            if value.get("test_case") == "delay":
                time.sleep(0.8)
            usage = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
            if options.get("observability"):
                print(f"srv load: spawning server instance with name={name} on port 1448", flush=True)
                prefix = f"[ 1448] 0.01.000.000 I slot print_timing: id  1 | task {task_id} | "
                for message in (
                    "processing task, is_child = 0",
                    "new prompt, n_ctx_slot = 4096, n_keep = 0, task.n_tokens = 120",
                    "prompt eval time = 100.00 ms / 80 tokens (1.25 ms per token, 800.00 tokens per second)",
                    "eval time = 200.00 ms / 30 tokens (6.67 ms per token, 150.00 tokens per second)",
                    "total time = 300.00 ms / 110 tokens",
                    "draft acceptance = 0.50000 (10 accepted / 20 generated), mean len = 2.00",
                    "stop processing: n_tokens = 150, truncated = 0",
                ):
                    print(prefix + message, flush=True)
                task_id += 1
            if value.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [{"choices": [{"delta": {"content": "private generated text"}}]}]
                if value.get("test_case") != "missing":
                    chunks.append({"choices": [], "usage": usage})
                if value.get("test_case") == "stream_error":
                    chunks.append({"error": "private upstream error"})
                for chunk in chunks:
                    raw = ("data: " + json.dumps(chunk) + "\r\n\r\n").encode()
                    self.wfile.write(raw[:7])
                    self.wfile.flush()
                    self.wfile.write(raw[7:])
                    self.wfile.flush()
                if value.get("test_case") != "truncated":
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                return
            result = {"choices": [{"message": {"content": "private generated text"}}]}
            if value.get("test_case") != "missing":
                result["usage"] = usage
            self.reply(200, result)

        def log_message(self, *args):
            pass

    with HTTPServer((args.host, args.port), Handler) as server:
        server.timeout = 0.05
        while True:
            server.handle_request()
            if "exit_after" in options and time.monotonic() - started >= options["exit_after"]:
                print("deliberate runtime failure", flush=True)
                return 7


if __name__ == "__main__":
    sys.exit(main())
