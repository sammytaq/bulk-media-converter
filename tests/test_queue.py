import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path

from PIL import Image

from converter import MediaTools
from queue_manager import QueueManager
from storage import AppStore
from tests.test_converter import test_tools


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = AppStore(self.root / "app-data")
        self.manager = QueueManager(self.store, MediaTools(None, None, frozenset()))

    def tearDown(self):
        self.manager.shutdown()
        self.temp.cleanup()

    def image(self, name="source.png", size=(80, 40)):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, "teal").save(path)
        return path

    def wait(self, condition):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.manager.snapshot()
            if condition(state):
                return state
            time.sleep(0.015)
        self.fail("Background operation did not finish within 10 seconds")

    def import_paths(self, paths):
        self.manager.add_paths([str(path) for path in paths])
        return self.wait(lambda state: not state["importing"])

    def test_recursive_import_deduplicates_and_skips_generated_folders(self):
        first = self.image()
        self.image("nested/second.png")
        self.image("converted/already-converted.png")
        (self.root / "ignored.txt").write_text("ignored")
        state = self.import_paths([self.root, first, self.root / "nested"])
        self.assertEqual(len(state["items"]), 2)
        self.assertEqual(
            {item["name"] for item in state["items"]}, {"source.png", "second.png"}
        )

    def test_corrupt_item_does_not_prevent_valid_conversion(self):
        valid = self.image()
        corrupt = self.root / "broken.png"
        corrupt.write_bytes(b"invalid png")
        self.import_paths([corrupt, valid])
        self.manager.start({"image_format": "webp"})
        state = self.wait(lambda value: not value["running"])
        statuses = {item["name"]: item["status"] for item in state["items"]}
        self.assertEqual(statuses, {"broken.png": "Error", "source.png": "Done"})
        self.assertTrue((self.root / "converted" / "source.webp").exists())

    def test_video_dependency_error_does_not_stop_image_job(self):
        image = self.image()
        video = self.root / "clip.mp4"
        video.write_bytes(b"missing ffmpeg")
        self.import_paths([video, image])
        self.manager.start({"image_format": "png"})
        state = self.wait(lambda value: not value["running"])
        self.assertEqual([item["status"] for item in state["items"]], ["Error", "Done"])
        self.assertIn("FFmpeg", state["items"][0]["error"])

    def test_per_item_overrides_apply_only_with_bulk_disabled(self):
        state = self.import_paths([self.image("one.png"), self.image("two.png")])
        self.manager.update_item(
            state["items"][0]["id"],
            {"preset": "custom", "image_format": "webp", "scale": 50},
        )
        self.manager.start({"image_format": "jpeg"}, bulk=False)
        final = self.wait(lambda value: not value["running"])
        with Image.open(final["items"][0]["output"]) as image:
            self.assertEqual(image.size, (40, 20))
            self.assertEqual(image.format, "WEBP")
        with Image.open(final["items"][1]["output"]) as image:
            self.assertEqual(image.size, (80, 40))
            self.assertEqual(image.format, "JPEG")

    def test_batch_copies_settings_before_worker_starts(self):
        self.import_paths([self.image()])
        settings = {"preset": "custom", "image_format": "webp", "scale": 50}
        self.manager.start(settings)
        settings.update(image_format="bmp", scale=25)
        state = self.wait(lambda value: not value["running"])
        with Image.open(state["items"][0]["output"]) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertEqual(image.size, (40, 20))

    def test_history_survives_store_reload(self):
        self.import_paths([self.image()])
        self.manager.start({"image_format": "png"})
        self.wait(lambda value: not value["running"])
        reloaded = AppStore(self.root / "app-data")
        history = reloaded.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "Done")
        self.assertTrue(Path(history[0]["output"]).exists())

    def test_unchanged_snapshots_avoid_retransmission(self):
        state = self.import_paths([self.image()])
        self.assertEqual(self.manager.snapshot(state["revision"]), {"unchanged": True})

    def test_metadata_privacy_setting_applies_to_per_item_overrides(self):
        source = self.root / "metadata.jpg"
        exif = Image.Exif()
        exif[315] = "Private camera owner"
        Image.new("RGB", (80, 40), "red").save(source, exif=exif)
        state = self.import_paths([source])
        self.manager.update_item(
            state["items"][0]["id"], {"image_format": "jpeg", "preserve_metadata": True}
        )
        self.manager.start(
            {"image_format": "jpeg", "preserve_metadata": False}, bulk=False
        )
        final = self.wait(lambda value: not value["running"])
        with Image.open(final["items"][0]["output"]) as image:
            self.assertNotIn(315, image.getexif())

    def test_completed_batch_reports_success_separately_from_later_imports(self):
        self.import_paths([self.image()])
        self.manager.start({"image_format": "png"})
        self.wait(lambda value: not value["running"])
        final = self.import_paths([self.image("later.png")])
        self.assertEqual(final["stats"]["pending"], 1)
        self.assertEqual(final["stats"]["batch_success"], 1)
        self.assertFalse(final["stats"]["batch_cancelled"])

    def test_custom_presets_and_preferences_persist(self):
        self.store.save_preset(
            "Small photos", {"preset": "custom", "image_format": "webp", "scale": 50}
        )
        self.store.update_preferences({"bulk": False})
        reloaded = AppStore(self.root / "app-data")
        self.assertEqual(reloaded.presets()[0]["name"], "Small photos")
        self.assertEqual(reloaded.presets()[0]["settings"]["scale"], 50)
        self.assertFalse(reloaded.preferences()["bulk"])

    def test_bad_state_file_recovers_with_defaults(self):
        directory = self.root / "damaged-state"
        directory.mkdir()
        (directory / "state.json").write_text("{broken")
        store = AppStore(directory)
        self.assertEqual(store.history(), [])
        store.update_preferences({"bulk": False})
        self.assertFalse(AppStore(directory).preferences()["bulk"])

    def test_audio_files_share_queue_and_respect_individual_format(self):
        tools = test_tools()
        if not tools.ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        self.manager.update_tools(tools)
        audio = self.root / "sound.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(22050)
            handle.writeframes(b"\0\0" * 22050)
        imported = self.import_paths([audio, self.image()])
        self.assertEqual(imported["stats"]["audios"], 1)
        self.assertEqual(imported["stats"]["images"], 1)
        self.manager.update_item(imported["items"][0]["id"], {"audio_format": "flac"})
        self.manager.start({"audio_format": "mp3"}, bulk=False)
        final = self.wait(lambda value: not value["running"])
        self.assertEqual([item["status"] for item in final["items"]], ["Done", "Done"])
        self.assertEqual(Path(final["items"][0]["output"]).suffix, ".flac")
        self.assertEqual(self.store.history()[-1]["target"], "flac")

    def test_audio_only_video_container_reclassifies_after_ffmpeg_setup(self):
        tools = test_tools()
        if not tools.ffmpeg:
            self.skipTest("FFmpeg is unavailable")
        source = self.root / "recording.mp4"
        subprocess.run(
            [
                tools.ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440",
                "-t",
                "0.5",
                "-c:a",
                "aac",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        imported = self.import_paths([source])
        self.assertEqual(imported["items"][0]["kind"], "video")
        self.manager.update_tools(tools)
        self.manager.start({"audio_format": "flac"})
        final = self.wait(lambda value: not value["running"])
        self.assertEqual(final["items"][0]["kind"], "audio")
        self.assertEqual(self.store.history()[0]["target"], "flac")


if __name__ == "__main__":
    unittest.main()
