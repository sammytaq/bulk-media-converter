import json
import subprocess
import sys
import tempfile
import time
import traceback
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webview
from PIL import Image

from app import DesktopAPI, create_window
from converter import CREATE_FLAGS
from storage import AppStore
from tests.test_converter import test_tools


def wait_until(condition, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError("Native UI condition timed out")


def main():
    outcomes = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        Image.new("RGBA", (80, 40), (0, 180, 160, 150)).save(root / "photo ü.png")
        Image.new("RGB", (48, 32), "coral").save(root / "another.jpg")
        with wave.open(str(root / "sound.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(22050)
            audio.writeframes(b"\0\0" * 22050)
        tools = test_tools()
        if not tools.ffmpeg:
            raise RuntimeError("Native smoke test requires FFmpeg")
        subprocess.run(
            [
                tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=96x64:rate=12",
                "-t",
                "0.8",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(root / "clip.mp4"),
            ],
            check=True,
            capture_output=True,
            creationflags=CREATE_FLAGS,
        )
        store = AppStore(root / "app-data")
        api = DesktopAPI(store)
        window = create_window(api)

        def run():
            try:
                wait_until(
                    lambda: window.evaluate_js(
                        "typeof state !== 'undefined' && state.ready && !!state.queue"
                    )
                )
                assert window.evaluate_js(
                    "!document.getElementById('ffmpeg-banner').hidden"
                )
                outcomes.append("Native WebView2 bridge and missing-FFmpeg banner")
                store.update_preferences({"ffmpeg_path": tools.ffmpeg})
                window.evaluate_js(
                    "document.querySelector('.rail [data-view=settings]').click(); document.getElementById('recheck-ffmpeg').click()"
                )
                wait_until(
                    lambda: window.evaluate_js("state.queue.capabilities.ffmpeg")
                )
                outcomes.append("FFmpeg settings recheck")
                paths = [
                    str(root / name)
                    for name in ("photo ü.png", "another.jpg", "clip.mp4", "sound.wav")
                ]
                window.evaluate_js(
                    f"window.pywebview.api.import_paths({json.dumps(paths)})"
                )
                wait_until(
                    lambda: window.evaluate_js(
                        "state.queue.items.length === 4 && !state.queue.importing"
                    )
                )
                window.evaluate_js(
                    "document.querySelector('.rail [data-view=converter]').click();document.getElementById('image-format').value='webp';document.getElementById('image-format').dispatchEvent(new Event('change'));document.getElementById('resolution').value=50;document.getElementById('resolution').dispatchEvent(new Event('change'))"
                )
                wait_until(
                    lambda: window.evaluate_js(
                        "state.settings.image_format === 'webp' && state.settings.scale === 50"
                    )
                )
                window.evaluate_js(
                    "document.querySelector('[data-filter=audio]').click()"
                )
                wait_until(
                    lambda: window.evaluate_js(
                        "document.querySelectorAll('.file-row').length === 1"
                    )
                )
                window.evaluate_js(
                    "document.querySelector('[data-filter=all]').click();document.getElementById('audio-format').value='flac';document.getElementById('audio-format').dispatchEvent(new Event('change'))"
                )
                wait_until(
                    lambda: window.evaluate_js("state.settings.audio_format === 'flac'")
                )
                outcomes.append(
                    "Audio queue filter and independent audio output selector"
                )
                window.evaluate_js(
                    "document.getElementById('save-preset').click();document.getElementById('preset-name').value='Native smoke preset';document.getElementById('preset-form').requestSubmit()"
                )
                wait_until(lambda: window.evaluate_js("state.presets.length === 1"))
                assert store.presets()[0]["settings"]["scale"] == 50
                outcomes.append(
                    "Native import, live settings, and persistent custom preset"
                )
                window.evaluate_js("document.getElementById('convert-all').click()")
                wait_until(
                    lambda: window.evaluate_js(
                        "state.queue.stats.done === 4 && !state.queue.running"
                    )
                )
                wait_until(
                    lambda: window.evaluate_js(
                        "!document.getElementById('completion').hidden && state.lastCompletedBatch === 1"
                    )
                )
                assert len(store.history()) == 4
                assert (root / "converted" / "sound.flac").is_file()
                with Image.open(root / "converted" / "photo ü.webp") as image:
                    assert image.size == (40, 20)
                outcomes.append(
                    "Real mixed-media batch, progress, and short-batch completion"
                )
                window.evaluate_js(
                    "document.querySelector('.rail [data-view=history]').click()"
                )
                wait_until(
                    lambda: window.evaluate_js(
                        "document.querySelectorAll('.history-row').length === 4"
                    )
                )
                window.evaluate_js(
                    "document.querySelector('.rail [data-view=converter]').click();document.getElementById('header-clear').click()"
                )
                wait_until(lambda: window.evaluate_js("state.queue.items.length === 0"))
                assert len(store.history()) == 4
                outcomes.append("History navigation and safe completed-queue clearing")
                report = {"ok": True, "checks": outcomes}
            except Exception:
                report = {
                    "ok": False,
                    "checks": outcomes,
                    "error": traceback.format_exc(),
                }
            Path("verification/native-report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            print(json.dumps(report), flush=True)
            window.destroy()

        try:
            webview.start(run, http_server=True, private_mode=True)
        finally:
            api._shutdown()
    if not json.loads(Path("verification/native-report.json").read_text())["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
