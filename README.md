# Bulk Media Converter

A local desktop app for bulk photo, video, and audio conversion. Python handles the files; a self-contained HTML/CSS/JS interface runs in pywebview. No uploads, accounts, web assets, or frontend build tools.

## Run

Requires Python 3.11 or newer. On Windows, Microsoft Edge WebView2 Runtime and .NET Framework 4.8 are required by pywebview. WebView2 is included on most current Windows installations; the [runtime installer](https://developer.microsoft.com/microsoft-edge/webview2/) is available from Microsoft.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

On Windows, double-click `launch.bat` after installation. It uses the local virtual environment when available. Files and folders can also be passed on the command line:

```powershell
.\.venv\Scripts\python.exe app.py "C:\Photos" "C:\Videos\clip.mov"
```

On macOS, install pywebview's Cocoa dependencies normally with pip. On Linux, install pywebview's [GTK or Qt requirements](https://pywebview.flowrl.com/guide/installation.html), then run `python app.py`.

## Enable video and audio

Install a full [FFmpeg build](https://ffmpeg.org/download.html) with `libx264`, `libvpx-vp9`, `mpeg4`, `flv`, `aac`, `libopus`, `libmp3lame`, `libvorbis`, `flac`, `wmav2`, and PCM encoders. Either add its `bin` directory to PATH or use **Settings → Choose FFmpeg** to select the executable. An optional `ffprobe` beside FFmpeg provides richer metadata; FFmpeg itself is used as a fallback. You can also set `MEDIA_CONVERTER_FFMPEG` to an executable path before launch.

The app shows a banner when FFmpeg is missing. Photos continue working. Video or audio that cannot be encoded gets its own error; the rest of the batch continues.

## Use

1. Drop multiple files or folders anywhere in the app, use **Add Photos / Videos / Audio**, or use the adjacent folder button. Folder imports include subfolders, skip symlink directories and generated `converted` subfolders, and deduplicate canonical paths.
2. Choose photo, video, and audio destinations independently. The search bar filters filenames, original formats, and statuses; queue tabs filter media type.
3. Choose **Same as source** for original dimensions at high quality, **Compress** for 75% resolution and quality, or **Match Output** for custom values. These presets control size/quality; the selected output format always applies. Maximum width `0` means unlimited. Images are never enlarged.
4. Leave **Bulk actions** enabled to use these settings for every pending file. Turn it off and select a filename to configure individual overrides. Items without overrides inherit the current defaults. Settings for a running batch are fixed; new imports wait for the next batch.
5. Choose an output folder, or keep the default `converted` directory next to each source. Existing output names get `_1`, `_2`, and so on. Originals are never replaced. Encoding uses temporary files and publishes completed output atomically.
6. Select **Convert all**. Stop conversion to cancel the active file and retain remaining files for retry. Photos can finish a native save before cancellation takes effect. Use **Retry failed** after addressing an error.
7. Open outputs with the header folder button, the completion shortcut, or a per-file history folder button. With multiple output locations, the main folder button opens the most recent location. History retains the last 200 attempts and lets you add originals to the queue again.

Custom presets are saved locally. The Settings view controls metadata retention, automatic folder opening, and FFmpeg selection. No source data is uploaded or deleted.

| Media | Input and output formats |
| --- | --- |
| Photos | JPG/JPEG, PNG, WEBP, BMP, TIFF/TIF, GIF, AVIF |
| Videos | MP4, MKV, AVI, MOV, WEBM, FLV |
| Audio | MP3, WAV, FLAC, M4A, AAC, OGG/OGA, OPUS, AIFF/AIF, WMA |

Image encoder availability follows the installed Pillow build; unavailable destinations are disabled. EXIF orientation is applied. JPEG and BMP composite transparent pixels onto white. GIF, animated WEBP/PNG/AVIF, and multipage TIFF retain multiple frames where supported. A static destination explicitly reports that it used the first frame. GIF has palette and transparency limitations. Large animations exceeding 2,000 frames or 400 million aggregate source pixels are rejected to bound memory.

Image quality controls JPEG, WEBP, AVIF, GIF palette size, and lossless PNG compression effort; TIFF and BMP remain lossless. Video quality maps to each encoder's quality/CRF setting. MP4/MOV/MKV use H.264/AAC, WEBM uses VP9/Opus, AVI uses MPEG-4/MP3, and FLV uses FLV1/MP3. Video conversion preserves the first video/audio tracks and source frame rate; subtitle and additional audio tracks are omitted. Outputs use compatible even pixel dimensions. Estimates start from source size/duration and adapt to observed batch progress; actual times depend on hardware and codecs.

Audio uses the same queue, cancellation, per-file overrides, presets, and history. MP3 uses LAME variable quality, M4A/AAC use AAC, OGG uses Vorbis, OPUS uses Opus, and WMA uses WMA v2. Quality controls lossy encoder quality/bitrate. WAV and AIFF retain source PCM precision when available; FLAC uses lossless integer encoding. Sample rate is retained when supported by the output codec; Opus uses 48 kHz and WMA uses 44.1 kHz. MP3/WMA downmix surround input to stereo; other outputs retain supported channels. Lossless encoding cannot restore detail already lost in a compressed source. Audio artwork is omitted; compatible text tags follow the metadata setting. Resolution controls apply only to photos/videos. Audio-only MP4/MKV files are recognized as audio when FFmpeg is available.

Preferences, presets, bounded history, and logs are stored under `%LOCALAPPDATA%\BulkMediaConverter` on Windows, `~/Library/Application Support/BulkMediaConverter` on macOS, and `$XDG_CONFIG_HOME/bulk-media-converter` on Linux. Queue contents are session-only. Clearing queue items or history never removes media files.

## Keyboard

| Shortcut | Action |
| --- | --- |
| Ctrl/Cmd+O | Add files |
| Ctrl/Cmd+Enter | Start or stop conversion |
| / | Focus search |
| Escape | Dismiss a dialog |

## Verify

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q app.py converter.py queue_manager.py storage.py
```

Tests create their own temporary image/video/audio fixtures. `imageio-ffmpeg` supplies an FFmpeg binary for development tests; it is excluded from the desktop release and is never downloaded at runtime. If neither it nor FFmpeg is available, video/audio integration tests explicitly skip. `TEST_FFMPEG` can point tests at a particular build.

`python tests/native_smoke.py` additionally opens a real WebView2 window and verifies a mixed image/video/audio batch through the native JavaScript bridge, including settings, presets, filters, completion, and history. Results are recorded under `verification/`.

## Package for Windows

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm BulkMediaConverter.spec
```

Distribute the entire `dist\Bulk Media Converter` folder; the executable is `Bulk Media Converter.exe`. The folder includes Python, pywebview, Pillow, and all UI assets. FFmpeg remains a separately installed dependency and can be selected in Settings. Build on each target operating system; the included spec targets Windows. Released binaries should be signed with your own certificate for distribution.

For development diagnostics, run `python app.py --debug` to enable WebView developer tools. Serve `ui/` with a static server for a layout-only browser preview; native file access requires the desktop app.
