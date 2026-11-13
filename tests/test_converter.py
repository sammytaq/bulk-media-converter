import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from converter import (
    Cancelled,
    ConversionError,
    ConversionSettings,
    MediaTools,
    convert_media,
    inspect_media,
)


def test_tools():
    executable = os.environ.get("TEST_FFMPEG") or shutil.which("ffmpeg")
    if not executable:
        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pass
    return MediaTools.detect(executable)


class ImageConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "photo ü.png"
        Image.new("RGBA", (80, 40), (255, 0, 0, 0)).save(self.source)
        self.tools = MediaTools(None, None, frozenset())

    def tearDown(self):
        self.temp.cleanup()

    def convert(self, **values):
        settings = ConversionSettings.from_dict({"preset": "custom", **values})
        return convert_media(
            self.source, settings, self.tools, lambda value: None, threading.Event()
        )

    def test_transparency_is_flattened_and_scaled(self):
        result = self.convert(image_format="jpeg", scale=50)
        with Image.open(result["output"]) as image:
            self.assertEqual(image.size, (40, 20))
            self.assertEqual(image.mode, "RGB")
            self.assertGreater(min(image.getpixel((0, 0))), 245)
        self.assertTrue(self.source.exists())

    def test_duplicate_outputs_never_overwrite(self):
        first = self.convert(image_format="png")
        original = Path(first["output"]).read_bytes()
        second = self.convert(image_format="png")
        self.assertEqual(Path(second["output"]).name, "photo ü_1.png")
        self.assertEqual(Path(first["output"]).read_bytes(), original)

    def test_source_directory_as_output_does_not_overwrite_source(self):
        original = self.source.read_bytes()
        result = self.convert(image_format="png", output_dir=str(self.root))
        self.assertEqual(Path(result["output"]).name, "photo ü_1.png")
        self.assertEqual(self.source.read_bytes(), original)

    def test_corrupt_file_leaves_no_partial_output(self):
        self.source.write_bytes(b"broken image")
        with self.assertRaises(ConversionError):
            self.convert()
        self.assertFalse(list(self.root.rglob("*.part.*")))
        self.assertFalse((self.root / "converted" / "photo ü.jpg").exists())

    def test_animated_gif_retains_frames(self):
        self.source = self.root / "animated.gif"
        frames = [
            Image.new("RGB", (40, 20), color) for color in ("red", "blue", "green")
        ]
        frames[0].save(
            self.source,
            save_all=True,
            append_images=frames[1:],
            duration=[100, 200, 300],
            loop=0,
        )
        result = self.convert(image_format="webp", quality=90)
        with Image.open(result["output"]) as image:
            self.assertEqual(image.n_frames, 3)
            self.assertEqual(image.size, (40, 20))

    def test_static_destination_reports_first_frame(self):
        self.source = self.root / "pages.tiff"
        frames = [Image.new("RGB", (30, 20), color) for color in ("red", "blue")]
        frames[0].save(self.source, save_all=True, append_images=frames[1:])
        result = self.convert(image_format="jpeg")
        self.assertIn("first frame", result["note"].lower())

    def test_exif_orientation_is_applied(self):
        self.source = self.root / "rotated.jpg"
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (80, 40), "red").save(self.source, exif=exif)
        result = self.convert(image_format="png")
        with Image.open(result["output"]) as image:
            self.assertEqual(image.size, (40, 80))
            self.assertNotIn(274, image.getexif())

    def test_pre_cancelled_job_creates_no_output(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            convert_media(
                self.source,
                ConversionSettings(),
                self.tools,
                lambda value: None,
                cancel,
            )
        self.assertFalse((self.root / "converted").exists())

    def test_invalid_settings_are_rejected(self):
        for values in (
            {"quality": 0},
            {"scale": 101},
            {"image_format": "exe"},
            {"video_format": "zip"},
            {"max_width": -1},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ConversionSettings.from_dict({"preset": "custom", **values})

    def test_supported_image_destinations_open_successfully(self):
        Image.init()
        for target in ("jpeg", "png", "webp", "bmp", "tiff", "gif", "avif"):
            if target.upper() not in Image.SAVE:
                continue
            with self.subTest(target=target):
                result = self.convert(image_format=target)
                with Image.open(result["output"]) as image:
                    image.load()
                    self.assertEqual(image.size, (80, 40))

    def test_transparent_png_remains_transparent_in_gif(self):
        result = self.convert(image_format="gif")
        with Image.open(result["output"]) as image:
            self.assertEqual(image.convert("RGBA").getpixel((0, 0))[3], 0)

    def test_sixteen_bit_grayscale_retains_tonal_range_in_jpeg(self):
        self.source = self.root / "sixteen-bit.png"
        image = Image.new("I;16", (100, 20))
        for x in range(100):
            for y in range(20):
                image.putpixel((x, y), round(x / 99 * 65535))
        image.save(self.source)
        result = self.convert(image_format="jpeg", quality=100)
        with Image.open(result["output"]) as output:
            for x, expected in ((0, 0), (25, 64), (50, 129), (75, 193), (99, 255)):
                self.assertLess(abs(output.getpixel((x, 10))[0] - expected), 5)

    def test_animated_png_to_gif_preserves_transparent_frames(self):
        self.source = self.root / "animated.png"
        first = Image.new("RGBA", (60, 20), (0, 0, 0, 0))
        second = first.copy()
        first.paste((255, 0, 0, 255), (4, 4, 15, 15))
        second.paste((0, 0, 255, 255), (40, 4, 51, 15))
        first.save(
            self.source,
            save_all=True,
            append_images=[second],
            duration=[100, 100],
            loop=0,
        )
        result = self.convert(image_format="gif")
        with Image.open(result["output"]) as output:
            self.assertEqual(output.n_frames, 2)
            self.assertEqual(output.convert("RGBA").getpixel((5, 5)), (255, 0, 0, 255))
            output.seek(1)
            rgba = output.convert("RGBA")
            self.assertEqual(rgba.getpixel((5, 5))[3], 0)
            self.assertEqual(rgba.getpixel((45, 5)), (0, 0, 255, 255))

    def test_concurrent_conversions_reserve_distinct_outputs(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            jobs = [workers.submit(self.convert, image_format="png") for _ in range(2)]
            outputs = [job.result()["output"] for job in jobs]
        self.assertEqual(len(set(outputs)), 2)
        for output in outputs:
            with Image.open(output) as image:
                self.assertEqual(image.size, (80, 40))


class AudioConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = test_tools()
        if not cls.tools.ffmpeg:
            raise unittest.SkipTest("FFmpeg is unavailable")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "sound ü.wav"
        subprocess.run(
            [
                self.tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "1",
                "-ac",
                "2",
                str(self.source),
            ],
            check=True,
            capture_output=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_audio_formats_are_playable_and_keep_duration(self):
        for target in (
            "mp3",
            "wav",
            "flac",
            "m4a",
            "aac",
            "ogg",
            "opus",
            "aiff",
            "wma",
        ):
            with self.subTest(target=target):
                progress = []
                settings = ConversionSettings.from_dict({"audio_format": target})
                result = convert_media(
                    self.source,
                    settings,
                    self.tools,
                    progress.append,
                    threading.Event(),
                )
                info = inspect_media(Path(result["output"]), self.tools)
                self.assertEqual(info["kind"], "audio")
                self.assertTrue(info["has_audio"])
                self.assertGreater(info["duration"], 0.8)
                self.assertGreater(info["sample_rate"], 0)
                self.assertEqual(info["channels"], 2)
                self.assertEqual(progress[-1], 100)
                decoded = subprocess.run(
                    [
                        self.tools.ffmpeg,
                        "-v",
                        "error",
                        "-i",
                        result["output"],
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                )
                self.assertEqual(
                    decoded.returncode, 0, decoded.stderr.decode("utf-8", "replace")
                )
                restored = convert_media(
                    Path(result["output"]),
                    ConversionSettings.from_dict({"audio_format": "wav"}),
                    self.tools,
                    lambda value: None,
                    threading.Event(),
                )
                self.assertGreater(Path(restored["output"]).stat().st_size, 1000)

    def test_audio_without_ffmpeg_is_actionable(self):
        with self.assertRaisesRegex(ConversionError, "FFmpeg"):
            convert_media(
                self.source,
                ConversionSettings(),
                MediaTools(None, None, frozenset()),
                lambda value: None,
                threading.Event(),
            )

    def test_cancelled_audio_cleans_up_output(self):
        cancel = threading.Event()

        def stop(progress):
            if progress > 0:
                cancel.set()

        with self.assertRaises(Cancelled):
            convert_media(self.source, ConversionSettings(), self.tools, stop, cancel)
        folder = self.root / "converted"
        self.assertFalse(list(folder.iterdir()) if folder.exists() else [])

    def test_twenty_four_bit_audio_keeps_precision_in_pcm_outputs(self):
        source = self.root / "high-resolution.wav"
        subprocess.run(
            [
                self.tools.ffmpeg,
                "-v",
                "error",
                "-i",
                str(self.source),
                "-c:a",
                "pcm_s24le",
                str(source),
            ],
            capture_output=True,
            check=True,
        )
        for target in ("wav", "aiff", "flac"):
            with self.subTest(target=target):
                result = convert_media(
                    source,
                    ConversionSettings.from_dict({"audio_format": target}),
                    self.tools,
                    lambda value: None,
                    threading.Event(),
                )
                info = inspect_media(Path(result["output"]), self.tools)
                self.assertEqual(info["bit_depth"], 24)

    def test_surround_2_1_audio_converts_to_opus(self):
        source = self.root / "surround.wav"
        subprocess.run(
            [
                self.tools.ffmpeg,
                "-v",
                "error",
                "-i",
                str(self.source),
                "-af",
                "pan=2.1|FL=c0|FR=c0|LFE=c0",
                "-c:a",
                "pcm_s24le",
                str(source),
            ],
            capture_output=True,
            check=True,
        )
        info = inspect_media(source, self.tools)
        self.assertEqual(info["channels"], 3)
        result = convert_media(
            source,
            ConversionSettings.from_dict({"audio_format": "opus"}),
            self.tools,
            lambda value: None,
            threading.Event(),
        )
        self.assertEqual(
            inspect_media(Path(result["output"]), self.tools)["channels"], 3
        )


class VideoConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = test_tools()
        if not cls.tools.ffmpeg:
            raise unittest.SkipTest("FFmpeg is unavailable")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "clip ü.mp4"
        subprocess.run(
            [
                self.tools.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=96x64:rate=12",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "0.8",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(self.source),
            ],
            check=True,
            capture_output=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_all_video_containers_produce_playable_scaled_media(self):
        for target in ("mp4", "mkv", "avi", "mov", "webm", "flv"):
            with self.subTest(target=target):
                progress = []
                settings = ConversionSettings.from_dict(
                    {"preset": "custom", "video_format": target, "scale": 50}
                )
                result = convert_media(
                    self.source,
                    settings,
                    self.tools,
                    progress.append,
                    threading.Event(),
                )
                info = inspect_media(Path(result["output"]), self.tools)
                self.assertEqual((info["width"], info["height"]), (48, 32))
                self.assertGreater(info["duration"], 0.4)
                self.assertTrue(info["has_audio"])
                self.assertEqual(progress[-1], 100)

    def test_missing_ffmpeg_is_actionable(self):
        with self.assertRaisesRegex(ConversionError, "FFmpeg"):
            convert_media(
                self.source,
                ConversionSettings(),
                MediaTools(None, None, frozenset()),
                lambda value: None,
                threading.Event(),
            )

    def test_cancellation_removes_incomplete_video(self):
        cancel = threading.Event()

        def stop(progress):
            if progress > 0:
                cancel.set()

        with self.assertRaises(Cancelled):
            convert_media(self.source, ConversionSettings(), self.tools, stop, cancel)
        output = self.root / "converted"
        self.assertFalse(list(output.iterdir()) if output.exists() else [])


if __name__ == "__main__":
    unittest.main()
