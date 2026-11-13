from __future__ import annotations

import argparse
import functools
import logging
import os
import subprocess
import sys
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import webview
from webview.dom import DOMEventHandler

from converter import (
    AUDIO_EXTENSIONS,
    CREATE_FLAGS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ConversionSettings,
    MediaTools,
)
from queue_manager import QueueManager
from storage import AppStore

VERSION = "1.0.0"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def guarded(method):
    @functools.wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            logging.exception("Desktop operation failed: %s", method.__name__)
            return {
                "ok": False,
                "error": str(exc) or "This operation could not be completed.",
            }

    return call


class DesktopAPI:
    def __init__(self, store: AppStore):
        self._store = store
        self._manager = QueueManager(
            store, MediaTools.detect(store.preferences().get("ffmpeg_path"))
        )
        self._window = None
        self._maximized = False

    def _bind(self, window):
        self._window = window

    @guarded
    def bootstrap(self):
        return {
            "ok": True,
            "version": VERSION,
            "state": self._manager.snapshot(),
            "preferences": self._store.preferences(),
            "history": self._store.history(),
            "presets": self._store.presets(),
        }

    @guarded
    def get_state(self, since=-1):
        return self._manager.snapshot(since)

    @guarded
    def get_thumbnail(self, identifier):
        return self._manager.thumbnail(identifier)

    @guarded
    def add_files(self):
        patterns = ";".join(
            f"*{extension}"
            for extension in sorted(
                IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
            )
        )
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=(
                f"Media files ({patterns})",
                "All files (*.*)",
            ),
        )
        return self._manager.add_paths(list(paths or []))

    @guarded
    def add_folders(self):
        paths = self._window.create_file_dialog(
            webview.FileDialog.FOLDER, allow_multiple=True
        )
        return self._manager.add_paths(list(paths or []))

    @guarded
    def import_paths(self, paths):
        return self._manager.add_paths(paths)

    @guarded
    def remove_file(self, identifier):
        return self._manager.remove(identifier)

    @guarded
    def clear_completed(self):
        return self._manager.clear_completed()

    @guarded
    def retry_errors(self):
        return self._manager.retry_errors()

    @guarded
    def set_item_settings(self, identifier, settings):
        return self._manager.update_item(identifier, settings)

    @guarded
    def start_conversion(self, settings, bulk=True):
        return self._manager.start(settings, bool(bulk))

    @guarded
    def cancel_conversion(self):
        return self._manager.cancel()

    @guarded
    def choose_output_folder(self):
        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return {"ok": True, "path": paths[0] if paths else None}

    @guarded
    def save_preferences(self, preferences):
        normalized = {}
        if "conversion" in preferences:
            normalized["conversion"] = ConversionSettings.from_dict(
                preferences["conversion"]
            ).to_dict()
        for key in ("bulk", "open_after"):
            if key in preferences:
                normalized[key] = bool(preferences[key])
        return {"ok": True, "preferences": self._store.update_preferences(normalized)}

    @guarded
    def save_preset(self, name, settings):
        return {"ok": True, "presets": self._store.save_preset(name, settings)}

    @guarded
    def delete_preset(self, name):
        self._store.delete_preset(name)
        return {"ok": True, "presets": self._store.presets()}

    @guarded
    def get_history(self):
        return {"ok": True, "history": self._store.history()}

    @guarded
    def clear_history(self):
        self._store.clear_history()
        return {"ok": True, "history": []}

    @guarded
    def requeue_history(self, identifier):
        record = next(
            (item for item in self._store.history() if item.get("id") == identifier),
            None,
        )
        if not record:
            raise ValueError("This history item is no longer available.")
        if not Path(record["path"]).is_file():
            raise ValueError("The original file has been moved or deleted.")
        state = self._manager.snapshot()
        existing = next(
            (item for item in state["items"] if item["path"] == record["path"]), None
        )
        if existing:
            self._manager.remove(existing["id"])
        return self._manager.add_paths([record["path"]])

    @guarded
    def check_ffmpeg(self):
        tools = MediaTools.detect(self._store.preferences().get("ffmpeg_path"))
        self._manager.update_tools(tools)
        return {"ok": True, "capabilities": tools.capabilities()}

    @guarded
    def choose_ffmpeg(self):
        file_types = (
            ("FFmpeg executable (*.exe)", "All files (*.*)")
            if os.name == "nt"
            else ("All files (*.*)",)
        )
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=file_types
        )
        if not paths:
            return {"ok": True, "cancelled": True}
        tools = MediaTools.detect(paths[0])
        if not tools.ffmpeg:
            raise ValueError("The selected file is not a working FFmpeg executable.")
        self._manager.update_tools(tools)
        self._store.update_preferences({"ffmpeg_path": tools.ffmpeg})
        return {"ok": True, "capabilities": tools.capabilities()}

    @guarded
    def use_system_ffmpeg(self):
        tools = MediaTools.detect()
        self._manager.update_tools(tools)
        self._store.update_preferences({"ffmpeg_path": ""})
        return {"ok": True, "capabilities": tools.capabilities()}

    def _open_folder(self, value: str):
        folder = Path(value).resolve()
        if not folder.is_dir():
            raise ValueError("This output folder has been moved or deleted.")
        if sys.platform == "win32":
            os.startfile(str(folder))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)], creationflags=CREATE_FLAGS)
        else:
            subprocess.Popen(["xdg-open", str(folder)], creationflags=CREATE_FLAGS)
        return {"ok": True, "path": str(folder)}

    @guarded
    def open_output(self, identifier=None):
        if identifier:
            item = next(
                (
                    item
                    for item in self._manager.snapshot()["items"]
                    if item["id"] == identifier
                ),
                None,
            )
            if not item:
                item = next(
                    (
                        item
                        for item in self._store.history()
                        if item.get("id") == identifier
                    ),
                    None,
                )
            if not item or not item.get("output"):
                raise ValueError("This file has no completed output yet.")
            return self._open_folder(str(Path(item["output"]).parent))
        folders = self._manager.output_directories()
        if folders:
            return self._open_folder(folders[-1])
        custom = self._store.preferences()["conversion"].get("output_dir")
        if custom and Path(custom).is_dir():
            return self._open_folder(custom)
        recent = next(
            (
                record
                for record in self._store.history()
                if record.get("output") and Path(record["output"]).parent.is_dir()
            ),
            None,
        )
        if recent:
            return self._open_folder(str(Path(recent["output"]).parent))
        raise ValueError("Convert a file or choose an output folder first.")

    @guarded
    def ffmpeg_help(self):
        webbrowser.open("https://ffmpeg.org/download.html")
        return {"ok": True}

    @guarded
    def minimize(self):
        self._window.minimize()
        return {"ok": True}

    @guarded
    def maximize(self):
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized
        return {"ok": True}

    @guarded
    def close(self):
        self._window.destroy()
        return {"ok": True}

    def _shutdown(self):
        self._manager.shutdown()


def create_window(api: DesktopAPI):
    screen = webview.screens[0]
    width = min(1440, max(600, screen.width - 64))
    height = min(960, max(500, screen.height - 80))
    window = webview.create_window(
        "Bulk Media Converter",
        url=str(ROOT / "ui" / "index.html"),
        js_api=api,
        width=width,
        height=height,
        min_size=(min(1060, width), min(740, height)),
        background_color="#1A1C1E",
        frameless=True,
        easy_drag=False,
        text_select=False,
    )
    api._bind(window)

    def on_drop(event):
        paths = [
            item.get("pywebviewFullPath")
            for item in event.get("dataTransfer", {}).get("files", [])
        ]
        paths = [path for path in paths if path]
        if paths:
            api.import_paths(paths)

    def on_loaded():
        window.dom.document.events.dragover += DOMEventHandler(
            lambda event: None, prevent_default=True, debounce=200
        )
        window.dom.document.events.drop += DOMEventHandler(
            on_drop, prevent_default=True
        )

    window.events.loaded += on_loaded
    window.events.closed += api._shutdown
    return window


def main():
    parser = argparse.ArgumentParser(description="Bulk Media Converter")
    parser.add_argument("files", nargs="*", help="Files or folders to add on startup")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    store = AppStore(args.data_dir)
    handler = RotatingFileHandler(
        store.directory / "app.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    logging.basicConfig(
        handlers=[handler],
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    api = DesktopAPI(store)
    create_window(api)
    if args.files:
        api.import_paths(args.files)
    try:
        webview.start(debug=args.debug, http_server=True, private_mode=True)
    finally:
        api._shutdown()


if __name__ == "__main__":
    main()
