from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from converter import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    Cancelled,
    ConversionError,
    ConversionSettings,
    MediaTools,
    convert_media,
    inspect_media,
    make_thumbnail,
    media_kind,
)
from storage import AppStore


class QueueManager:
    def __init__(self, store: AppStore, tools: MediaTools):
        self.store = store
        self.tools = tools
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._paths: set[str] = set()
        self._revision = 0
        self._imports = 0
        self._running = False
        self._closed = False
        self._notice = ""
        self._cancel = threading.Event()
        self._importer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="media-import"
        )
        self._worker: threading.Thread | None = None
        self._batch_ids: set[str] = set()
        self._batch_total = 0
        self._batch_done = 0
        self._batch_started = 0.0
        self._batch_fraction = 0.0
        self._batch_success = 0
        self._batch_errors = 0
        self._batch_cancelled = False
        self._batch_id = 0

    def _touch(self):
        self._revision += 1

    def _walk(self, paths: list[str]):
        for value in paths:
            if self._closed:
                return
            path = Path(value).expanduser()
            if path.is_dir():
                for root, directories, files in os.walk(
                    path, followlinks=False, onerror=self._walk_error
                ):
                    directories[:] = sorted(
                        directory
                        for directory in directories
                        if directory.casefold() != "converted"
                        and not (Path(root) / directory).is_symlink()
                    )
                    for name in sorted(files):
                        if self._closed:
                            return
                        yield Path(root) / name
            else:
                yield path

    def _walk_error(self, error):
        with self._lock:
            self._notice = f"Some files could not be accessed: {error}"
            self._touch()

    def add_paths(self, paths: list[str]) -> dict:
        if not isinstance(paths, list) or not all(
            isinstance(value, str) for value in paths
        ):
            raise ValueError("Choose files or folders to import.")
        if not paths:
            return {"ok": True}
        with self._lock:
            if self._closed:
                raise ValueError("The application is closing.")
            self._imports += 1
            self._notice = ""
            self._touch()
            self._importer.submit(self._import_paths, paths[:])
        return {"ok": True}

    def _import_paths(self, paths: list[str]):
        added = skipped = errors = 0
        try:
            for path in self._walk(paths):
                if (
                    path.suffix.lower()
                    not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
                ):
                    skipped += 1
                    continue
                canonical = os.path.normcase(str(path.resolve()))
                with self._lock:
                    if canonical in self._paths:
                        skipped += 1
                        continue
                    self._paths.add(canonical)
                identifier = uuid.uuid4().hex
                item = {
                    "id": identifier,
                    "name": path.name,
                    "path": str(path.resolve()),
                    "canonical": canonical,
                    "kind": media_kind(path),
                    "format": path.suffix[1:].upper(),
                    "size": 0,
                    "width": 0,
                    "height": 0,
                    "duration": 0,
                    "status": "Pending",
                    "progress": 0,
                    "error": "",
                    "note": "",
                    "output": "",
                    "output_size": 0,
                    "settings": None,
                    "_thumbnail": None,
                }
                try:
                    info = inspect_media(path, self.tools)
                    item.update(info)
                    item["_thumbnail"] = make_thumbnail(path, self.tools, info)
                except (ConversionError, OSError) as exc:
                    item.update(status="Error", error=str(exc))
                    errors += 1
                with self._lock:
                    if self._closed:
                        return
                    self._items[identifier] = item
                    added += 1
                    self._touch()
        except Exception as exc:
            with self._lock:
                self._notice = f"Import interrupted: {exc}"
        finally:
            with self._lock:
                self._imports -= 1
                self._notice = (
                    f"Added {added} file{'s' if added != 1 else ''}"
                    + (
                        f" · {skipped} unsupported or duplicate skipped"
                        if skipped
                        else ""
                    )
                    + (f" · {errors} could not be read" if errors else "")
                )
                self._touch()

    def snapshot(self, since: int | None = None) -> dict:
        with self._lock:
            if since == self._revision:
                return {"unchanged": True}
            items = [
                {
                    **{
                        key: copy.deepcopy(value)
                        for key, value in item.items()
                        if not key.startswith("_") and key != "canonical"
                    },
                    "has_thumbnail": bool(item["_thumbnail"]),
                    "locked": self._running
                    and identifier in self._batch_ids
                    and item["status"] not in {"Done", "Error"},
                }
                for identifier, item in self._items.items()
            ]
            counts = {
                status: sum(item["status"] == status for item in items)
                for status in ("Pending", "Converting...", "Done", "Error")
            }
            fraction = (
                (self._batch_done + self._batch_fraction) / self._batch_total
                if self._batch_total
                else 0
            )
            elapsed = time.monotonic() - self._batch_started if self._running else 0
            estimated = sum(
                max(
                    0.3,
                    item["duration"] * 0.8
                    if item["kind"] in {"video", "audio"} and item["duration"]
                    else item["size"] / 6_000_000,
                )
                for item in items
                if item["status"] == "Pending"
            )
            if self._running and fraction > 0.03 and elapsed > 1:
                estimated = elapsed * (1 - fraction) / fraction
            return {
                "revision": self._revision,
                "items": items,
                "importing": self._imports > 0,
                "running": self._running,
                "cancelling": self._running and self._cancel.is_set(),
                "notice": self._notice,
                "capabilities": self.tools.capabilities(),
                "stats": {
                    "total": len(items),
                    "pending": counts["Pending"],
                    "done": counts["Done"],
                    "errors": counts["Error"],
                    "bytes": sum(item["size"] for item in items),
                    "images": sum(item["kind"] == "image" for item in items),
                    "videos": sum(item["kind"] == "video" for item in items),
                    "audios": sum(item["kind"] == "audio" for item in items),
                    "eta": max(0, round(estimated)),
                    "batch_progress": round(min(1, fraction) * 100),
                    "batch_total": self._batch_total,
                    "batch_done": self._batch_done,
                    "batch_success": self._batch_success,
                    "batch_errors": self._batch_errors,
                    "batch_cancelled": self._batch_cancelled,
                    "batch_id": self._batch_id,
                },
            }

    def thumbnail(self, identifier: str) -> str | None:
        with self._lock:
            return self._items.get(identifier, {}).get("_thumbnail")

    def update_tools(self, tools: MediaTools):
        with self._lock:
            if self._running:
                raise ValueError("Stop the current batch before changing FFmpeg.")
            self.tools = tools
            self._touch()

    def update_item(self, identifier: str, settings: dict) -> dict:
        normalized = ConversionSettings.from_dict(settings).to_dict()
        with self._lock:
            item = self._items.get(identifier)
            if not item:
                raise ValueError("This file is no longer in the queue.")
            if self._running and identifier in self._batch_ids:
                raise ValueError("Settings are locked for the current batch.")
            item["settings"] = normalized
            self._touch()
        return {"ok": True}

    def remove(self, identifier: str) -> dict:
        with self._lock:
            item = self._items.get(identifier)
            if item:
                if (
                    self._running
                    and identifier in self._batch_ids
                    and item["status"] not in {"Done", "Error"}
                ):
                    raise ValueError("Stop the batch before removing a waiting file.")
                self._paths.discard(item["canonical"])
                del self._items[identifier]
                self._touch()
        return {"ok": True}

    def clear_completed(self) -> dict:
        with self._lock:
            for identifier in [
                identifier
                for identifier, item in self._items.items()
                if item["status"] == "Done"
            ]:
                self.remove(identifier)
        return {"ok": True}

    def retry_errors(self) -> dict:
        with self._lock:
            if self._running:
                raise ValueError("Wait for the batch to finish before retrying files.")
            for item in self._items.values():
                if item["status"] == "Error":
                    item.update(status="Pending", progress=0, error="", note="")
            self._touch()
        return {"ok": True}

    def start(self, settings: dict, bulk: bool = True) -> dict:
        normalized = ConversionSettings.from_dict(settings)
        with self._lock:
            if self._closed or self._running:
                raise ValueError(
                    "A conversion is already running or the app is closing."
                )
            if self._imports:
                raise ValueError(
                    "Wait for the import to finish before starting conversion."
                )
            jobs = [
                (
                    identifier,
                    Path(item["path"]),
                    normalized
                    if bulk or not item["settings"]
                    else ConversionSettings.from_dict(
                        {
                            **normalized.to_dict(),
                            **item["settings"],
                            "preserve_metadata": normalized.preserve_metadata,
                        }
                    ),
                )
                for identifier, item in self._items.items()
                if item["status"] == "Pending"
            ]
            if not jobs:
                raise ValueError("Add files or retry failed files to start a batch.")
            self._cancel.clear()
            self._running = True
            self._batch_ids = {job[0] for job in jobs}
            self._batch_total = len(jobs)
            self._batch_done = 0
            self._batch_success = 0
            self._batch_errors = 0
            self._batch_cancelled = False
            self._batch_id += 1
            self._batch_fraction = 0
            self._batch_started = time.monotonic()
            self._notice = ""
            self._touch()
            self._worker = threading.Thread(
                target=self._run, args=(jobs, self.tools), name="media-conversion"
            )
            self._worker.start()
        return {"ok": True, "count": len(jobs)}

    def _progress(self, identifier: str, value: float):
        with self._lock:
            if identifier in self._items:
                self._items[identifier]["progress"] = round(value, 1)
                self._batch_fraction = value / 100
                self._touch()

    def _run(self, jobs: list, tools: MediaTools):
        try:
            for identifier, path, settings in jobs:
                if self._cancel.is_set():
                    break
                with self._lock:
                    item = self._items.get(identifier)
                    if not item:
                        continue
                    item.update(status="Converting...", progress=0, error="", note="")
                    self._batch_fraction = 0
                    self._touch()
                try:
                    result = convert_media(
                        path,
                        settings,
                        tools,
                        lambda value, key=identifier: self._progress(key, value),
                        self._cancel,
                    )
                    with self._lock:
                        item.update(result, status="Done", progress=100)
                except Cancelled:
                    with self._lock:
                        item.update(
                            status="Pending",
                            progress=0,
                            note="Stopped. Ready to retry.",
                        )
                    break
                except Exception as exc:
                    with self._lock:
                        item.update(
                            status="Error",
                            progress=0,
                            error=str(exc) or type(exc).__name__,
                        )
                with self._lock:
                    record = {
                        key: item.get(key)
                        for key in (
                            "id",
                            "name",
                            "path",
                            "kind",
                            "format",
                            "size",
                            "status",
                            "error",
                            "note",
                            "output",
                            "output_size",
                        )
                    }
                    record.update(
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        target={
                            "image": settings.image_format,
                            "video": settings.video_format,
                            "audio": settings.audio_format,
                        }[item["kind"]],
                    )
                    try:
                        self.store.add_history(record)
                    except OSError as exc:
                        self._notice = f"Conversion finished, but history could not be saved: {exc}"
                    self._batch_done += 1
                    self._batch_success += int(item["status"] == "Done")
                    self._batch_errors += int(item["status"] == "Error")
                    self._batch_fraction = 0
                    self._touch()
        finally:
            with self._lock:
                self._running = False
                self._batch_cancelled = (
                    self._cancel.is_set() and self._batch_done < self._batch_total
                )
                self._batch_fraction = 0
                self._batch_ids.clear()
                self._touch()

    def cancel(self) -> dict:
        with self._lock:
            self._cancel.set()
            self._touch()
        return {"ok": True}

    def output_directories(self) -> list[str]:
        with self._lock:
            return list(
                dict.fromkeys(
                    str(Path(item["output"]).parent)
                    for item in self._items.values()
                    if item["output"]
                )
            )

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._cancel.set()
        self._importer.shutdown(wait=True, cancel_futures=True)
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join()
