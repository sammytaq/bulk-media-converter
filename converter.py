from __future__ import annotations

import base64
import io
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif"}
)
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv"})
AUDIO_EXTENSIONS = frozenset(
    {
        ".mp3",
        ".wav",
        ".flac",
        ".m4a",
        ".aac",
        ".ogg",
        ".oga",
        ".opus",
        ".aiff",
        ".aif",
        ".wma",
        ".mpga",
    }
)
IMAGE_FORMATS = ("jpeg", "png", "webp", "bmp", "tiff", "gif", "avif")
VIDEO_FORMATS = ("mp4", "mkv", "avi", "mov", "webm", "flv")
AUDIO_FORMATS = ("mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "aiff", "wma")
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class ConversionError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class ConversionSettings:
    image_format: str = "jpeg"
    video_format: str = "mp4"
    audio_format: str = "mp3"
    preset: str = "source"
    scale: int = 100
    quality: int = 95
    max_width: int = 0
    output_dir: str = ""
    preserve_metadata: bool = True

    @classmethod
    def from_dict(cls, data: dict | None = None) -> ConversionSettings:
        data = data or {}
        values = {
            key: value for key, value in data.items() if key in cls.__dataclass_fields__
        }
        for key in ("image_format", "video_format", "audio_format", "preset"):
            if key in values:
                values[key] = str(values[key]).lower()
        if values.get("image_format") == "jpg":
            values["image_format"] = "jpeg"
        if values.get("image_format") == "tif":
            values["image_format"] = "tiff"
        for key in ("scale", "quality", "max_width"):
            if key in values:
                try:
                    values[key] = int(values[key])
                except (ValueError, TypeError, OverflowError) as exc:
                    raise ValueError(
                        f"{key.replace('_', ' ').title()} must be a whole number."
                    ) from exc
        settings = cls(**values)
        if (
            settings.image_format not in IMAGE_FORMATS
            or settings.video_format not in VIDEO_FORMATS
            or settings.audio_format not in AUDIO_FORMATS
        ):
            raise ValueError("Choose a supported output format.")
        if settings.preset not in {"source", "compress", "custom"}:
            raise ValueError("Choose a valid preset.")
        if not 1 <= settings.quality <= 100:
            raise ValueError("Quality must be between 1 and 100.")
        if not 25 <= settings.scale <= 100:
            raise ValueError("Resolution must be between 25 and 100%.")
        if settings.max_width != 0 and not 64 <= settings.max_width <= 16384:
            raise ValueError(
                "Maximum width must be 0 (unlimited) or between 64 and 16384 pixels."
            )
        if not isinstance(settings.output_dir, str) or "\0" in settings.output_dir:
            raise ValueError("Choose a valid output folder.")
        if not isinstance(settings.preserve_metadata, bool):
            raise ValueError("Preserve metadata must be enabled or disabled.")
        if settings.preset == "source":
            return cls(
                **{**asdict(settings), "scale": 100, "quality": 95, "max_width": 0}
            )
        if settings.preset == "compress":
            return cls(
                **{**asdict(settings), "scale": 75, "quality": 75, "max_width": 0}
            )
        return settings

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: str | None
    ffprobe: str | None
    encoders: frozenset[str]

    @classmethod
    def detect(cls, executable: str | None = None) -> MediaTools:
        candidate = (
            executable
            or os.environ.get("MEDIA_CONVERTER_FFMPEG")
            or shutil.which("ffmpeg")
        )
        if not candidate:
            return cls(None, None, frozenset())
        resolved = shutil.which(str(candidate)) or str(Path(candidate).expanduser())
        try:
            version = subprocess.run(
                [resolved, "-version"],
                capture_output=True,
                timeout=8,
                creationflags=CREATE_FLAGS,
            )
            if (
                version.returncode != 0
                or b"ffmpeg version" not in version.stdout.lower()
            ):
                return cls(None, None, frozenset())
            result = subprocess.run(
                [resolved, "-hide_banner", "-encoders"],
                capture_output=True,
                timeout=8,
                creationflags=CREATE_FLAGS,
            )
            encoders = frozenset(
                line.split()[1]
                for line in result.stdout.decode("utf-8", "replace").splitlines()
                if re.match(r"^\s*[VAS][A-Z.]{5}\s+\S+", line)
            )
            sibling = Path(resolved).with_name(
                "ffprobe.exe" if os.name == "nt" else "ffprobe"
            )
            probe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
            return cls(str(Path(resolved).resolve()), probe, encoders)
        except (OSError, subprocess.SubprocessError):
            return cls(None, None, frozenset())

    def capabilities(self) -> dict:
        Image.init()
        return {
            "ffmpeg": bool(self.ffmpeg),
            "ffmpeg_path": self.ffmpeg or "",
            "ffprobe": bool(self.ffprobe),
            "image_formats": [
                name for name in IMAGE_FORMATS if name.upper() in Image.SAVE
            ],
            "video_formats": list(VIDEO_FORMATS),
            "audio_formats": list(AUDIO_FORMATS),
        }


def media_kind(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    raise ConversionError(f"Unsupported format: {extension or 'no file extension'}")


def _number(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def _channel_count(layout: str) -> int:
    known = {
        "mono": 1,
        "stereo": 2,
        "quad": 4,
        "hexagonal": 6,
        "octagonal": 8,
        "downmix": 2,
    }
    if layout in known:
        return known[layout]
    explicit = re.search(r"\b(\d+) channels?\b", layout)
    if explicit:
        return int(explicit[1])
    surround = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", layout)
    return sum(int(part or 0) for part in surround.groups()) if surround else 0


def inspect_media(path: Path, tools: MediaTools) -> dict:
    path = Path(path)
    kind = media_kind(path)
    try:
        info = {
            "kind": kind,
            "format": path.suffix[1:].upper(),
            "size": path.stat().st_size,
            "width": 0,
            "height": 0,
            "duration": 0.0,
            "frames": 1,
            "has_audio": False,
            "sample_rate": 0,
            "channels": 0,
            "channel_layout": "",
            "bit_depth": 0,
            "audio_codec": "",
        }
        if kind == "image":
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    image.load()
                    width, height = image.size
                    if image.getexif().get(274) in {5, 6, 7, 8}:
                        width, height = height, width
                    info.update(
                        width=width, height=height, frames=getattr(image, "n_frames", 1)
                    )
            return info
        if not tools.ffmpeg:
            return info
        if tools.ffprobe:
            result = subprocess.run(
                [
                    tools.ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                timeout=20,
                creationflags=CREATE_FLAGS,
            )
            if result.returncode:
                raise ConversionError(result.stderr.decode("utf-8", "replace")[-1000:])
            data = json.loads(result.stdout)
            audio = next(
                (
                    stream
                    for stream in data.get("streams", [])
                    if stream.get("codec_type") == "audio"
                ),
                None,
            )
            video = next(
                (
                    stream
                    for stream in data.get("streams", [])
                    if stream.get("codec_type") == "video"
                    and not stream.get("disposition", {}).get("attached_pic")
                ),
                None,
            )
            if kind == "audio" or (not video and audio):
                if not audio:
                    raise ConversionError(
                        "This file does not contain a readable audio stream."
                    )
                info.update(
                    kind="audio",
                    duration=_number(
                        data.get("format", {}).get("duration"),
                        _number(audio.get("duration")),
                    ),
                    has_audio=True,
                    sample_rate=int(audio.get("sample_rate", 0)),
                    channels=int(audio.get("channels", 0)),
                    channel_layout=audio.get("channel_layout", ""),
                    bit_depth=int(_number(audio.get("bits_per_raw_sample")))
                    or int(_number(audio.get("bits_per_sample"))),
                    audio_codec=audio.get("codec_name", ""),
                )
                return info
            if not video:
                raise ConversionError(
                    "This file does not contain a readable video stream."
                )
            width, height = int(video.get("width", 0)), int(video.get("height", 0))
            rotation = next(
                (
                    side.get("rotation", 0)
                    for side in video.get("side_data_list", [])
                    if "rotation" in side
                ),
                video.get("tags", {}).get("rotate", 0),
            )
            if abs(_number(rotation)) % 180 == 90:
                width, height = height, width
            info.update(
                width=width,
                height=height,
                duration=_number(
                    data.get("format", {}).get("duration"),
                    _number(video.get("duration")),
                ),
                has_audio=any(
                    stream.get("codec_type") == "audio"
                    for stream in data.get("streams", [])
                ),
            )
        else:
            result = subprocess.run(
                [tools.ffmpeg, "-hide_banner", "-i", str(path)],
                capture_output=True,
                timeout=20,
                creationflags=CREATE_FLAGS,
            )
            details = result.stderr.decode("utf-8", "replace")
            duration = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", details)
            seconds = (
                int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
                if duration
                else 0.0
            )
            audio_line = next(
                (line for line in details.splitlines() if "Audio:" in line), ""
            )
            video_line = next(
                (
                    line
                    for line in details.splitlines()
                    if "Video:" in line and "attached pic" not in line
                ),
                "",
            )
            dimensions = re.search(r"\b(\d{2,6})x(\d{2,6})\b", video_line)
            if kind == "audio" or (not dimensions and audio_line):
                if not audio_line:
                    raise ConversionError(
                        "Cannot read audio. The file may be damaged or use an unsupported codec."
                    )
                sample_rate = re.search(r"(\d+)\s*Hz", audio_line)
                layout = (
                    audio_line.split("Hz,", 1)[1].split(",", 1)[0].strip()
                    if "Hz," in audio_line
                    else ""
                )
                codec = re.search(r"Audio:\s*([^\s,]+)", audio_line)
                depth = (
                    re.search(r"\((\d+) bit\)", audio_line)
                    or re.search(r"pcm_[sfu](\d+)", audio_line)
                    or re.search(r",\s*[su](8|16|32|64)(?:p)?\b", audio_line)
                )
                info.update(
                    kind="audio",
                    duration=seconds,
                    has_audio=True,
                    sample_rate=int(sample_rate[1]) if sample_rate else 0,
                    channels=_channel_count(layout),
                    channel_layout=layout,
                    bit_depth=int(depth[1]) if depth else 0,
                    audio_codec=codec[1] if codec else "",
                )
                return info
            if not dimensions:
                raise ConversionError(
                    "Cannot read video. The file may be damaged or use an unsupported codec."
                )
            width, height = map(int, dimensions.groups())
            rotation = re.search(r"rotation of\s+(-?[\d.]+)", details)
            if rotation and abs(_number(rotation.group(1))) % 180 == 90:
                width, height = height, width
            duration = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", details)
            seconds = (
                int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
                if duration
                else 0.0
            )
            info.update(
                width=width,
                height=height,
                duration=seconds,
                has_audio="Audio:" in details,
            )
        if not info["width"] or not info["height"]:
            raise ConversionError("The video has invalid dimensions.")
        return info
    except ConversionError:
        raise
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        subprocess.SubprocessError,
    ) as exc:
        raise ConversionError(f"Could not read {path.name}: {exc}") from exc


def make_thumbnail(path: Path, tools: MediaTools, info: dict) -> str | None:
    if info["kind"] == "audio":
        return None
    try:
        if info["kind"] == "video":
            if not tools.ffmpeg:
                return None
            result = subprocess.run(
                [
                    tools.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(min(1, info.get("duration", 0) / 4)),
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=144:104:force_original_aspect_ratio=decrease",
                    "-f",
                    "image2pipe",
                    "-c:v",
                    "mjpeg",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=15,
                creationflags=CREATE_FLAGS,
            )
            if result.returncode or not result.stdout:
                return None
            data = result.stdout
        else:
            with Image.open(path) as source:
                source.thumbnail((144, 104), Image.Resampling.LANCZOS)
                image = _flatten(ImageOps.exif_transpose(source))
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=75)
                data = buffer.getvalue()
        return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        Image.DecompressionBombError,
    ):
        return None


def _eight_bit(image: Image.Image) -> Image.Image:
    if image.mode.startswith("I;16") or (
        image.mode == "I" and 255 < image.getextrema()[1] <= 65535
    ):
        return image.convert("I").point(lambda value: value / 257).convert("L")
    return image


def _gif_palette(image: Image.Image, quality: int) -> Image.Image:
    rgba = _eight_bit(image).convert("RGBA")
    mask = rgba.getchannel("A").point(lambda value: 255 if value < 128 else 0)
    transparent = mask.getbbox() is not None
    colors = max(2, round(256 * quality / 100))
    result = rgba.convert("RGB").quantize(
        colors=colors - int(transparent), method=Image.Quantize.MEDIANCUT
    )
    if transparent:
        index = colors - 1
        result.paste(index, mask=mask)
        result.info["transparency"] = index
    return result


def _flatten(image: Image.Image) -> Image.Image:
    image = _eight_bit(image)
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _dimensions(
    width: int, height: int, settings: ConversionSettings, even=False
) -> tuple[int, int]:
    ratio = min(
        settings.scale / 100, settings.max_width / width if settings.max_width else 1, 1
    )
    target = max(1, round(width * ratio)), max(1, round(height * ratio))
    if even:
        return max(2, target[0] // 2 * 2), max(2, target[1] // 2 * 2)
    return target


def _check_cancel(cancel: threading.Event):
    if cancel.is_set():
        raise Cancelled("Conversion stopped.")


@contextmanager
def _destination(source: Path, settings: ConversionSettings, extension: str):
    folder = (
        Path(settings.output_dir).expanduser().resolve()
        if settings.output_dir
        else source.parent / "converted"
    )
    folder.mkdir(parents=True, exist_ok=True)
    index = 0
    while True:
        suffix = f"_{index}" if index else ""
        target = folder / f"{source.stem}{suffix}.{extension}"
        try:
            with target.open("xb"):
                pass
            break
        except FileExistsError:
            index += 1
    temporary = folder / f".{target.stem}.{uuid.uuid4().hex}.part.{extension}"
    committed = False
    try:
        yield target, temporary
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ConversionError("The encoder did not produce an output file.")
        os.replace(temporary, target)
        committed = True
    finally:
        temporary.unlink(missing_ok=True)
        if not committed:
            target.unlink(missing_ok=True)


def _convert_image(
    source: Path,
    destination: Path,
    settings: ConversionSettings,
    progress: Callable,
    cancel: threading.Event,
) -> str:
    target = settings.image_format.upper()
    Image.init()
    if target not in Image.SAVE:
        raise ConversionError(
            f"{target} encoding is unavailable in this Pillow installation."
        )
    frames = []
    try:
        with Image.open(source) as image:
            count = getattr(image, "n_frames", 1)
            preserve_frames = target in {"GIF", "WEBP", "PNG", "TIFF", "AVIF"}
            selected_count = count if preserve_frames else 1
            if (
                selected_count > 2000
                or image.width * image.height * selected_count > 400_000_000
            ):
                raise ConversionError(
                    "This animation is too large to convert safely in memory. Choose a static format or split the source."
                )
            metadata = {}
            durations = []
            loop = image.info.get("loop", 0)
            for index in range(selected_count):
                _check_cancel(cancel)
                image.seek(index)
                frame = ImageOps.exif_transpose(image)
                if target not in {"PNG", "TIFF"}:
                    frame = _eight_bit(frame)
                if (
                    index == 0
                    and settings.preserve_metadata
                    and target in {"JPEG", "PNG", "WEBP", "TIFF", "AVIF"}
                ):
                    if frame.info.get("icc_profile"):
                        metadata["icc_profile"] = frame.info["icc_profile"]
                    exif = frame.getexif()
                    if exif:
                        metadata["exif"] = exif.tobytes()
                dimensions = _dimensions(frame.width, frame.height, settings)
                if frame.size != dimensions:
                    frame = frame.resize(dimensions, Image.Resampling.LANCZOS)
                if target in {"JPEG", "BMP"}:
                    frame = _flatten(frame)
                elif target == "GIF":
                    frame = _gif_palette(frame, settings.quality)
                elif target != "TIFF" and frame.mode not in {
                    "RGB",
                    "RGBA",
                    "L",
                    "LA",
                    "P",
                    "I;16",
                    "I",
                }:
                    frame = frame.convert("RGBA" if "A" in frame.getbands() else "RGB")
                if target in {"WEBP", "AVIF"} and frame.mode not in {"RGB", "RGBA"}:
                    frame = frame.convert("RGBA")
                frames.append(frame.copy())
                durations.append(image.info.get("duration", 100))
                progress(10 + 60 * (index + 1) / selected_count)
            options = dict(metadata)
            if target in {"JPEG", "WEBP", "AVIF"}:
                options["quality"] = settings.quality
            if target == "JPEG":
                options.update(optimize=True, progressive=True)
            elif target == "PNG":
                options["compress_level"] = 9 if settings.quality < 80 else 6
            elif target == "WEBP":
                options["method"] = 5
            elif target == "TIFF":
                options["compression"] = "tiff_deflate"
            if len(frames) > 1:
                options.update(save_all=True, append_images=frames[1:])
                if target != "TIFF":
                    options.update(duration=durations, loop=loop)
                if target == "GIF":
                    options["disposal"] = 2
            _check_cancel(cancel)
            progress(78)
            frames[0].save(destination, target, **options)
            _check_cancel(cancel)
            progress(98)
            return (
                f"First frame only; {target} does not support multiple frames."
                if count > 1 and not preserve_frames
                else ""
            )
    finally:
        for frame in frames:
            frame.close()


def _video_command(
    source: Path,
    destination: Path,
    settings: ConversionSettings,
    tools: MediaTools,
    info: dict,
) -> list[str]:
    target = settings.video_format
    video_encoder = {"webm": "libvpx-vp9", "avi": "mpeg4", "flv": "flv"}.get(
        target, "libx264"
    )
    audio_encoder = {"webm": "libopus", "avi": "libmp3lame", "flv": "libmp3lame"}.get(
        target, "aac"
    )
    required = {video_encoder} | ({audio_encoder} if info.get("has_audio") else set())
    missing = required - tools.encoders
    if tools.encoders and missing:
        raise ConversionError(
            f"This FFmpeg build is missing encoder(s): {', '.join(sorted(missing))}. Install a full FFmpeg build."
        )
    width, height = _dimensions(info["width"], info["height"], settings, even=True)
    command = [
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-sn",
        "-dn",
        "-map_metadata",
        "0" if settings.preserve_metadata else "-1",
        "-map_chapters",
        "-1",
        "-vf",
        f"scale={width}:{height}:flags=lanczos",
        "-c:v",
        video_encoder,
        "-pix_fmt",
        "yuv420p",
    ]
    if video_encoder == "libx264":
        command += [
            "-preset",
            "medium",
            "-crf",
            str(round(18 + (100 - settings.quality) * 0.32)),
        ]
    elif video_encoder == "libvpx-vp9":
        command += [
            "-crf",
            str(round(15 + (100 - settings.quality) * 0.45)),
            "-b:v",
            "0",
            "-deadline",
            "good",
            "-cpu-used",
            "2",
            "-row-mt",
            "1",
        ]
    else:
        command += ["-q:v", str(max(2, round(2 + (100 - settings.quality) * 0.29)))]
    command += [
        "-c:a",
        audio_encoder,
        "-b:a",
        "192k" if audio_encoder == "aac" else "128k",
    ]
    if target == "flv":
        command += ["-ar", "44100"]
    if target in {"mp4", "mov"}:
        command += ["-movflags", "+faststart"]
    return command + ["-progress", "pipe:1", "-nostats", str(destination)]


def _audio_command(
    source: Path,
    destination: Path,
    settings: ConversionSettings,
    tools: MediaTools,
    info: dict,
) -> list[str]:
    target = settings.audio_format
    encoder = {
        "mp3": "libmp3lame",
        "wav": "pcm_s16le",
        "flac": "flac",
        "m4a": "aac",
        "aac": "aac",
        "ogg": "libvorbis",
        "opus": "libopus",
        "aiff": "pcm_s16be",
        "wma": "wmav2",
    }[target]
    if target in {"wav", "aiff"}:
        depth = info.get("bit_depth") or 16
        depth = 16 if depth <= 16 else 24 if depth <= 24 else 32 if depth <= 32 else 64
        floating = info.get("audio_codec", "").startswith("pcm_f")
        encoder = (
            f"pcm_{'f' if floating else 's'}{depth}{'le' if target == 'wav' else 'be'}"
        )
    if tools.encoders and encoder not in tools.encoders:
        raise ConversionError(
            f"This FFmpeg build is missing the {encoder} audio encoder. Install a full FFmpeg build."
        )
    command = [
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "0" if settings.preserve_metadata else "-1",
        "-map_chapters",
        "-1",
        "-c:a",
        encoder,
    ]
    if target == "mp3":
        command += ["-q:a", str(round((100 - settings.quality) * 9 / 99))]
        if info.get("channels", 0) > 2:
            command += ["-ac", "2"]
    elif target == "ogg":
        command += ["-q:a", f"{(settings.quality - 1) * 10 / 99:.1f}"]
    elif target in {"m4a", "aac", "opus", "wma"}:
        maximum = 192 if target == "wma" else 256 if target == "opus" else 320
        bitrate = round((32 + (settings.quality / 100) ** 2 * (maximum - 32)) / 8) * 8
        command += ["-b:a", f"{bitrate}k"]
        if target == "wma":
            command += ["-ar", "44100"]
            if info.get("channels", 0) > 2:
                command += ["-ac", "2"]
        elif target == "opus":
            layout = info.get("channel_layout", "")
            family = (
                0
                if info.get("channels", 0) <= 2
                else 1
                if layout in {"3.0", "quad", "5.0", "5.1", "6.1", "7.1"}
                else 255
            )
            command += ["-mapping_family", str(family)]
    elif target == "flac":
        command += ["-compression_level", "8"]
    if not settings.preserve_metadata:
        command += ["-map_metadata:s", "-1"]
    if target == "m4a":
        command += ["-movflags", "+faststart"]
    return command + ["-progress", "pipe:1", "-nostats", str(destination)]


def _encode_ffmpeg(
    command: list[str],
    seconds_total: float,
    progress: Callable,
    cancel: threading.Event,
):
    lines = queue.SimpleQueue()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors, creationflags=CREATE_FLAGS
        )

        def read_progress():
            for line in iter(process.stdout.readline, b""):
                lines.put(line.decode("utf-8", "replace").strip())

        reader = threading.Thread(
            target=read_progress, name="ffmpeg-progress", daemon=True
        )
        reader.start()
        try:
            progress(1)
            while True:
                _check_cancel(cancel)
                while not lines.empty():
                    line = lines.get()
                    if line.startswith("out_time_us=") and seconds_total > 0:
                        seconds = _number(line.split("=", 1)[1]) / 1_000_000
                        progress(min(98, max(1, seconds / seconds_total * 98)))
                if process.poll() is not None:
                    break
                cancel.wait(0.1)
            if process.returncode:
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 1800))
                details = errors.read().decode("utf-8", "replace").strip()
                raise ConversionError(
                    f"FFmpeg could not convert this media: {details or 'unsupported codec or invalid media'}"
                )
            _check_cancel(cancel)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=2)
            process.stdout.close()


def convert_media(
    source: Path,
    settings: ConversionSettings,
    tools: MediaTools,
    progress: Callable[[float], None],
    cancel: threading.Event,
) -> dict:
    source = Path(source).resolve()
    _check_cancel(cancel)
    try:
        info = inspect_media(source, tools)
        if info["kind"] in {"video", "audio"} and not tools.ffmpeg:
            raise ConversionError(
                "FFmpeg is required for video and audio conversion. Install it on PATH or select it in Settings."
            )
        extension = {
            "image": settings.image_format,
            "video": settings.video_format,
            "audio": settings.audio_format,
        }[info["kind"]]
        extension = {"jpeg": "jpg"}.get(extension, extension)
        note = ""
        _check_cancel(cancel)
        with _destination(source, settings, extension) as (target, temporary):
            if info["kind"] == "image":
                note = _convert_image(source, temporary, settings, progress, cancel)
            else:
                command = (
                    _audio_command(source, temporary, settings, tools, info)
                    if info["kind"] == "audio"
                    else _video_command(source, temporary, settings, tools, info)
                )
                _encode_ffmpeg(command, info["duration"], progress, cancel)
            _check_cancel(cancel)
        progress(100)
        return {
            "output": str(target),
            "output_size": target.stat().st_size,
            "note": note,
            **{
                key: info[key]
                for key in (
                    "kind",
                    "width",
                    "height",
                    "duration",
                    "sample_rate",
                    "channels",
                )
            },
        }
    except (Cancelled, ConversionError):
        raise
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        subprocess.SubprocessError,
    ) as exc:
        raise ConversionError(str(exc)) from exc
