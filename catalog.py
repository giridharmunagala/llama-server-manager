"""Managed copies of llama.cpp model presets."""

import configparser
import io
import json
import os
from pathlib import Path
import re
import tempfile

from manager import ManagerError


def atomic_text(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ManagerError(f"Cannot save {path.name}: {exc}", 500) from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def parser():
    result = configparser.ConfigParser(interpolation=None, delimiters=("=",),
                                       comment_prefixes=("#", ";"), strict=True)
    result.optionxform = str
    return result


def validate_options(options):
    if not isinstance(options, dict):
        raise ManagerError("Model options must be an object of string values.")
    reserved = {"host", "port", "api-key", "api-key-file", "models-dir", "models-preset",
                "models-max", "models-autoload", "no-models-autoload", "alias", "a",
                "ssl-key-file", "ssl-cert-file", "hf-token", "hft", "llama-api-key", "log-file"}
    for key, value in options.items():
        if not isinstance(key, str):
            raise ManagerError("Model option names must be strings.")
        canonical = key.lower().removeprefix("llama_arg_").replace("_", "-")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", key) or canonical in reserved
                or not isinstance(value, str) or not value.strip()
                or any(char in value for char in "\r\n\0")):
            raise ManagerError(f"Invalid or manager-controlled option: {key}")
    return dict(options)


class Catalog:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "catalog.json"
        self.active_path = Path(data_dir) / "router-active.ini"
        self.value = {"source": str(Path.home() / "models.ini"), "defaults": {},
                      "models": {}, "enabled": [], "max_loaded": 1, "autoload": True}
        if self.path.exists():
            self.value = self.validate(json.loads(self.path.read_text(encoding="utf-8")))

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) != {
                "source", "defaults", "models", "enabled", "max_loaded", "autoload"}:
            raise ManagerError("Invalid catalog fields.")
        if not isinstance(value["source"], str) or "\0" in value["source"]:
            raise ManagerError("Preset source must be a path.")
        defaults = validate_options(value["defaults"])
        models = value["models"]
        if not isinstance(models, dict):
            raise ManagerError("Models must be an object.")
        clean = {}
        for name, options in models.items():
            if (not isinstance(name, str) or not name.strip() or name in {"*", "DEFAULT"}
                    or any(char in name for char in "[]\r\n\0")):
                raise ManagerError("Invalid model section name.")
            clean[name] = validate_options(options)
            merged = {**defaults, **clean[name]}
            if not any(merged.get(key) for key in
                       ("model", "m", "hf-repo", "hf", "LLAMA_ARG_MODEL", "LLAMA_ARG_HF_REPO")):
                raise ManagerError(f"{name} needs a model path or hf-repo.")
        enabled = value["enabled"]
        if (not isinstance(enabled, list) or any(not isinstance(name, str) for name in enabled)
                or len(set(enabled)) != len(enabled) or not set(enabled) <= set(clean)):
            raise ManagerError("Enabled models must be unique catalog model names.")
        if type(value["max_loaded"]) is not int or not 1 <= value["max_loaded"] <= 32:
            raise ManagerError("Maximum loaded models must be between 1 and 32.")
        if type(value["autoload"]) is not bool:
            raise ManagerError("Autoload must be a boolean.")
        return {**value, "defaults": defaults, "models": clean, "enabled": list(enabled)}

    def save(self, value):
        clean = self.validate(value)
        atomic_text(self.path, json.dumps(clean, indent=2) + "\n")
        self.value = clean
        return clean

    def import_file(self, source):
        if not isinstance(source, str) or not source.strip() or "\0" in source:
            raise ManagerError("Choose a preset INI file.")
        path = Path(source).expanduser().resolve()
        try:
            text = path.read_text(encoding="utf-8-sig")
            if len(text) > 1024 * 1024:
                raise ManagerError("Preset file exceeds 1 MiB.")
            ini = parser()
            ini.read_string("[__metadata__]\n" + text)
            if ini.defaults():
                raise ManagerError("Use [*] for defaults, not [DEFAULT].")
            metadata = dict(ini["__metadata__"])
            if metadata and metadata != {"version": "1"}:
                raise ManagerError("Only preset version = 1 is supported.")
            models = {}
            defaults = {}
            for name in ini.sections():
                if name == "__metadata__":
                    continue
                options = dict(ini[name])
                # Router IDs are the section names; llama.cpp controls aliases itself.
                options.pop("alias", None)
                if name == "*":
                    defaults = options
                else:
                    models[name] = options
        except (OSError, UnicodeError, configparser.Error) as exc:
            raise ManagerError(f"Cannot import presets: {exc}") from exc
        return self.save({**self.value, "source": str(path), "defaults": defaults,
                          "models": models, "enabled": list(models)})

    def write_active(self):
        if not self.value["enabled"]:
            raise ManagerError("Enable at least one model for the router.")
        ini = parser()
        ini["*"] = self.value["defaults"]
        for name in self.value["enabled"]:
            ini[name] = self.value["models"][name]
        output = io.StringIO()
        ini.write(output)
        atomic_text(self.active_path, "version = 1\n\n" + output.getvalue())
        return str(self.active_path)
