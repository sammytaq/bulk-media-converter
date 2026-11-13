# Bulk Media Converter Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deliver a complete native desktop bulk image/video converter with the requested dashboard.

**Architecture:** `converter.py` handles inspection, thumbnails, safe outputs, and encoding. `queue_manager.py` owns asynchronous import/conversion and snapshots. `app.py` exposes native dialogs and the Python bridge; `storage.py` persists preferences/history. `ui/index.html` contains all frontend CSS and JS.

**Tech Stack:** Python 3.11+, pywebview 6+, Pillow 12+, subprocess, FFmpeg, unittest.

**Spec:** `docs/superpowers/specs/2026-11-12-bulk-media-converter-design.md`

## Global Constraints

- Match the supplied dark charcoal/teal dashboard and bundle the supplied logo.
- All conversion and import work stays off the UI thread.
- Never overwrite source files or preexisting outputs; clean incomplete files.
- No inline explanatory comments in product code; all assets work offline.
- Missing FFmpeg prevents only video work; images remain available.

## Review Focus

- Duplicate names, Unicode, and concurrent output reservation retain existing data.
- Corrupt media and absent codecs produce a per-item error and continue the batch.
- Cancellation stops the active process and leaves pending files retryable.
- GIF/TIFF animation/pages and alpha/orientation survive supported conversions.
- Imports, settings edits, and removals during a batch cannot mutate its settings or crash it.

## Task 1: Media engine

**Files:** `converter.py`, `tests/test_converter.py`, `requirements.txt`, `requirements-dev.txt`, `.gitignore`.

**Interfaces:** `ConversionSettings.from_dict(data)`, `inspect_media(path, tools)`, `convert_media(path, settings, tools, progress, cancel)`, `MediaTools.detect(executable=None)`.

- [x] Write real-fixture tests for scaled transparent PNG→JPEG, repeated filenames, corrupt input cleanup, animation, and cancellation. Run and observe missing implementation failures.
- [x] Implement validated settings, tool detection, media inspection, thumbnails, image/video conversion, safe atomic output, and progress callbacks.
- [x] Run image tests plus real generated video conversions in all six containers with available FFmpeg. Confirm dimensions, frames/audio, and nonzero output.

## Task 2: Background queue and persistence

**Files:** `queue_manager.py`, `storage.py`, `tests/test_queue.py`.

**Interfaces:** `QueueManager.add_paths(paths)`, `snapshot()`, `start(settings, bulk=True)`, `cancel()`, `remove(id)`, `update_item(id, settings)`, `shutdown()`. Snapshot includes items, importing/running, stats, capabilities, and revision.

- [x] Write failing tests for recursive deduplicated imports, mixed corrupt/valid batches, fixed batch settings, and persisted history.
- [x] Implement bounded/background imports, per-item state, sequential worker, cancellation, ETA, errors, overrides, and atomic JSON preferences/history.
- [x] Run the full unittest suite and verify worker shutdown and output cleanup.

## Task 3: Desktop bridge and complete frontend

**Files:** `app.py`, `ui/index.html`, `ui/assets/logo.png`, `launch.bat`, `README.md`, `BulkMediaConverter.spec`.

**Interfaces:** JS uses `window.pywebview.api` for snapshot, import dialogs, folder selection, conversion, presets/settings/history, and native output-folder controls.

- [x] Build and inspect a real empty state, populated queue, progress/error states, history, presets, settings, keyboard access, and responsive layout.
- [x] Wire native file/folder dialogs and full-path drop events using the documented pywebview DOM API. Prevent browser navigation on drops.
- [x] Supply a native launcher, packaging setup, and concise installation/run documentation.
- [x] Run Python compile checks, the full suite, browser interaction checks, and native pywebview smoke tests.

## Task 4: Review and delivery

- [x] Dispatch one independent whole-project review, address material findings, and re-run relevant checks.
- [x] Verify no network assets, accidental user-file changes, dead controls, or incomplete output artifacts.
- [x] Record final test evidence and deliver the app with run instructions.

## Task 5: User-requested audio support

**Files:** `converter.py`, `queue_manager.py`, `app.py`, `ui/index.html`, `tests/test_converter.py`, `tests/test_queue.py`, `tests/native_smoke.py`, `README.md`.

- [x] Add validated audio settings, inspection, all nine output encoders, shared cancellation/progress, sample rate/channel information, and safe audio outputs.
- [x] Add audio import, selector, queue filter/count, per-item settings, preset persistence, history targets, and FFmpeg dependency messaging.
- [x] Verify actual encode/decode in every audio format, 24-bit PCM preservation, surround Opus conversion, missing FFmpeg, cancellation, and mixed queues.
- [x] Re-run native mixed image/video/audio workflow and packaged application checks.
