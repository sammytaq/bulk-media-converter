from __future__ import annotations

import copy
import json
import os
import sys
import threading
import uuid
from pathlib import Path

from converter import ConversionSettings


def app_data_directory() -> Path:
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "BulkMediaConverter"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BulkMediaConverter"
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "bulk-media-converter"
    )


class AppStore:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory else app_data_directory()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self._lock = threading.RLock()
        self._data = {
            "preferences": {
                "conversion": ConversionSettings().to_dict(),
                "bulk": True,
                "ffmpeg_path": "",
                "open_after": False,
            },
            "history": [],
            "presets": [],
        }
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                if isinstance(loaded.get("preferences"), dict):
                    self._data["preferences"].update(loaded["preferences"])
                for key in ("history", "presets"):
                    if isinstance(loaded.get(key), list):
                        self._data[key] = [
                            item for item in loaded[key] if isinstance(item, dict)
                        ][-200:]
                try:
                    self._data["preferences"]["conversion"] = (
                        ConversionSettings.from_dict(
                            self._data["preferences"]["conversion"]
                        ).to_dict()
                    )
                except (ValueError, TypeError, AttributeError):
                    self._data["preferences"]["conversion"] = (
                        ConversionSettings().to_dict()
                    )
        except (OSError, ValueError):
            pass

    def _write(self, updated: dict):
        temporary = self.directory / f".state-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._data = updated
        finally:
            temporary.unlink(missing_ok=True)

    def preferences(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data["preferences"])

    def update_preferences(self, preferences: dict) -> dict:
        with self._lock:
            updated = copy.deepcopy(self._data)
            updated["preferences"].update(preferences)
            self._write(updated)
            return copy.deepcopy(updated["preferences"])

    def history(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(list(reversed(self._data["history"])))

    def add_history(self, record: dict):
        with self._lock:
            updated = copy.deepcopy(self._data)
            updated["history"] = (updated["history"] + [record])[-200:]
            self._write(updated)

    def clear_history(self):
        with self._lock:
            self._write({**self._data, "history": []})

    def presets(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._data["presets"])

    def save_preset(self, name: str, settings: dict) -> list[dict]:
        name = str(name).strip()
        if not name or len(name) > 60:
            raise ValueError("Enter a preset name between 1 and 60 characters.")
        normalized = ConversionSettings.from_dict(settings).to_dict()
        with self._lock:
            updated = copy.deepcopy(self._data)
            presets = [
                preset
                for preset in updated["presets"]
                if preset.get("name", "").casefold() != name.casefold()
            ]
            if len(presets) >= 30:
                raise ValueError(
                    "You can save up to 30 custom presets. Remove one before adding another."
                )
            presets.append({"name": name, "settings": normalized})
            updated["presets"] = presets
            self._write(updated)
            return copy.deepcopy(presets)

    def delete_preset(self, name: str):
        with self._lock:
            self._write(
                {
                    **self._data,
                    "presets": [
                        preset
                        for preset in self._data["presets"]
                        if preset.get("name") != name
                    ],
                }
            )
