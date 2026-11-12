"""Clip Maker core logic (no GUI code here, so it can be tested headlessly).

Main entry points:
    check_tools()          -> which external tools (ffmpeg, JS runtime) were found
    fetch_info(url)        -> VideoInfo (title, duration, thumbnail...)
    ClipJob(...).run()     -> downloads ONLY the chosen section, returns output path
    update_downloader()    -> pip-upgrades yt-dlp inside this venv

The actual download runs in a child process (`python core.py --worker ...`) so
"Cancel" can kill yt-dlp *and* the ffmpeg process it starts, instantly and cleanly.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

APP_NAME = "ClipMaker"

# Extra places Homebrew puts binaries. Apps started from Finder get a very short
# PATH, so we add these ourselves.
_EXTRA_BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
                   str(Path.home() / ".deno" / "bin")]
for _d in _EXTRA_BIN_DIRS:
    if os.path.isdir(_d) and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _d

AUDIO_QUALITIES = ["128", "192", "320"]           # kbps
VIDEO_QUALITIES = ["Best", "1080p", "720p", "480p"]


class ClipError(Exception):
    """An error with a message that is safe to show to the user."""


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_output_dir() -> str:
    d = Path.home() / "Downloads"
    return str(d if d.is_dir() else Path.home())


def load_config() -> dict:
    cfg = {"output_dir": default_output_dir(), "kind": "MP4",
           "audio_quality": "192", "video_quality": "1080p", "cookies_browser": "None"}
    try:
        cfg.update(json.loads((config_dir() / "config.json").read_text()))
    except Exception:
        pass
    if not os.path.isdir(cfg.get("output_dir", "")):
        cfg["output_dir"] = default_output_dir()
    return cfg


def save_config(cfg: dict) -> None:
    try:
        (config_dir() / "config.json").write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def parse_time(text: str) -> float:
    """'1:02:03', '2:03', '45', '1:05.5' -> seconds. Raises ValueError."""
    t = (text or "").strip()
    if not t:
        raise ValueError("empty time")
    parts = t.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad time: {text!r}")
    total = 0.0
    for i, p in enumerate(parts):
        p = p.strip()
        if not re.fullmatch(r"\d+(\.\d+)?", p):
            raise ValueError(f"bad time: {text!r}")
        v = float(p)
        if i > 0 and v >= 60:
            raise ValueError(f"minutes/seconds must be under 60: {text!r}")
        total = total * 60 + v
    return total


def format_time(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _stamp(sec: float) -> str:
    """Filename-safe time stamp: 65 -> '1m05s', 3725 -> '1h02m05s'."""
    sec = max(0, int(round(sec)))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name or "clip")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:max_len].rstrip(" .") or "clip")


def validate_range(start: float, end: float, duration: Optional[float]) -> None:
    if start < 0:
        raise ClipError("Start time can't be negative.")
    if end <= start:
        raise ClipError("End time must be after the start time.")
    if duration and end > duration + 0.5:
        raise ClipError(f"End time is past the end of the video ({format_time(duration)}).")
    if end - start < 0.5:
        raise ClipError("The clip must be at least half a second long.")


# --------------------------------------------------------------------------- #
# External tools
# --------------------------------------------------------------------------- #
def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def find_js_runtimes() -> dict:
    """YouTube now needs a JavaScript runtime for yt-dlp to see all formats."""
    rts = {}
    for name in ("deno", "node", "bun"):
        p = shutil.which(name)
        if p:
            rts[name] = {"path": p}
    return rts


def check_tools() -> dict:
    return {"ffmpeg": find_ffmpeg(), "js": find_js_runtimes()}


def ytdlp_version() -> str:
    try:
        out = subprocess.run([sys.executable, "-c",
                              "import yt_dlp.version as v; print(v.__version__)"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def update_downloader() -> tuple[bool, str]:
    """pip-upgrade yt-dlp inside the running venv. Returns (ok, message)."""
    before = ytdlp_version()
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade",
                            "--disable-pip-version-check", "yt-dlp[default]"],
                           capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, f"Update failed: {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        return False, "Update failed: " + " ".join(tail)
    after = ytdlp_version()
    if after == before:
        return True, f"Downloader is up to date (yt-dlp {after})."
    return True, f"Updated yt-dlp {before} → {after}. Restart Clip Maker to use it."


# --------------------------------------------------------------------------- #
# Friendly errors
# --------------------------------------------------------------------------- #
def friendly_error(msg: str) -> str:
    m = re.sub(r"\x1b\[[0-9;]*m", "", str(msg))
    low = m.lower()
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "ffmpeg is missing. Open Terminal and run:  brew install ffmpeg"
    if "private video" in low:
        return "This video is private, so it can't be downloaded."
    if "confirm your age" in low or "age-restricted" in low or "age restricted" in low \
            or "inappropriate for some users" in low:
        return ("This video is age-restricted. Pick your browser under "
                "'Use cookies from' (where you're signed in to YouTube) and try again.")
    if "not a bot" in low:
        return ("YouTube asked to confirm you're not a bot. Try 'Update downloader', "
                "or pick your browser under 'Use cookies from' and try again.")
    if "403" in low or "forbidden" in low:
        return ("YouTube refused the download (error 403). Click 'Update downloader' and try "
                "again. If it keeps happening, pick your browser under 'Use cookies from'.")
    if "members-only" in low or "join this channel" in low:
        return "This video is for channel members only."
    if "is live" in low or "live event" in low or "premieres in" in low:
        return "Live streams and upcoming premieres can't be clipped yet."
    if "unsupported url" in low or "is not a valid url" in low or "incomplete youtube id" in low \
            or "invalid url" in low:
        return "That doesn't look like a YouTube video link. Copy the link from the address bar and try again."
    if "http error 404" in low or "[generic]" in low:
        return "That page doesn't have a video Clip Maker can read. Copy the link of a YouTube video and try again."
    if "video unavailable" in low or "video is unavailable" in low or "removed" in low or "does not exist" in low:
        return "This video is unavailable (removed, region-blocked, or the link is wrong)."
    if "copyright" in low:
        return "This video was taken down for copyright reasons."
    if any(s in low for s in ("unable to download webpage", "getaddrinfo", "nodename nor servname",
                              "timed out", "connection", "network is unreachable",
                              "temporary failure in name resolution", "urlopen error",
                              "http error 5", "ssl")):
        return "Network problem: couldn't reach YouTube. Check your internet connection and try again."
    if "could not copy" in low and "cookie" in low or "cookies" in low and "database" in low:
        return ("Couldn't read that browser's cookies. Quit the browser and try again, "
                "or set 'Use cookies from' back to None.")
    if "requested format is not available" in low:
        return "That quality isn't available for this video. Try 'Best' or a lower quality."
    first = m.strip().splitlines()[0] if m.strip() else "Unknown error"
    first = re.sub(r"^ERROR:\s*", "", first)
    return f"Something went wrong: {first[:300]}"


def _looks_like_url(url: str) -> bool:
    return bool(re.match(r"^https?://\S+$", url.strip())) or \
        bool(re.match(r"^(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/\S+$", url.strip()))


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ClipError("Paste a YouTube link first.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not _looks_like_url(url):
        raise ClipError("That doesn't look like a link. It should start with https://")
    return url


# --------------------------------------------------------------------------- #
# yt-dlp options
# --------------------------------------------------------------------------- #
class _QuietLogger:
    """Keeps yt-dlp from printing to the Terminal; errors still reach us as exceptions."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _base_opts(cookies_browser: str = "None") -> dict:
    opts = {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,          # a watch?v=..&list=.. link means just that video
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
    }
    js = find_js_runtimes()
    if js:
        opts["js_runtimes"] = js
    ff = find_ffmpeg()
    if ff:
        opts["ffmpeg_location"] = ff
    if cookies_browser and cookies_browser.lower() not in ("none", ""):
        opts["cookiesfrombrowser"] = (cookies_browser.lower(),)
    return opts


@dataclass
class VideoInfo:
    url: str
    id: str
    title: str
    duration: Optional[float]
    thumbnail: Optional[str]
    uploader: str = ""
    is_live: bool = False
    raw_formats: list = field(default_factory=list, repr=False)
    thumbnails: list = field(default_factory=list, repr=False)


def fetch_info(url: str, cookies_browser: str = "None") -> VideoInfo:
    import yt_dlp
    url = normalize_url(url)
    opts = _base_opts(cookies_browser)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info)
    except Exception as e:  # yt_dlp.utils.DownloadError and friends
        raise ClipError(friendly_error(str(e))) from e
    if info is None:
        raise ClipError("Couldn't read that video.")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ClipError("That's a playlist link. Open a single video and copy its link.")
        info = entries[0]
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise ClipError("Live streams and upcoming premieres can't be clipped yet.")
    # Thumbnail candidates: small, reliable JPGs first (maxres often 404s on old videos).
    thumbs = []
    if info.get("id") and "youtube" in (info.get("extractor") or "").lower():
        thumbs += [f"https://i.ytimg.com/vi/{info['id']}/{n}.jpg"
                   for n in ("hqdefault", "mqdefault", "default")]
    if info.get("thumbnail"):
        thumbs.append(info["thumbnail"])
    return VideoInfo(
        url=info.get("webpage_url") or url,
        id=info.get("id", ""),
        title=info.get("title") or "clip",
        duration=float(info["duration"]) if info.get("duration") else None,
        thumbnail=thumbs[0] if thumbs else None,
        thumbnails=thumbs,
        uploader=info.get("uploader") or info.get("channel") or "",
        is_live=bool(info.get("is_live")),
        raw_formats=info.get("formats") or [],
    )


def fetch_thumbnail(info: "VideoInfo") -> Optional[bytes]:
    for u in info.thumbnails or ([info.thumbnail] if info.thumbnail else []):
        try:
            data = fetch_thumbnail_bytes(u)
            if data:
                return data
        except Exception:
            continue
    return None


def fetch_thumbnail_bytes(url: str, timeout: float = 15) -> bytes:
    ctx = None
    try:
        import certifi  # python.org Python on macOS ships without root certs
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def format_selector(kind: str, quality: str) -> str:
    if kind == "MP3":
        return "ba[ext=m4a]/ba/b"
    h = {"1080p": 1080, "720p": 720, "480p": 480}.get(quality)
    lim = f"[height<={h}]" if h else ""
    # Prefer H.264 (avc1) + AAC (m4a) so QuickTime/Photos/iMovie can play it with
    # no re-encode; fall back to anything (we re-encode to H.264 afterwards).
    return (f"bv*{lim}[vcodec^=avc1]+ba[ext=m4a]/"
            f"b{lim}[vcodec^=avc1][acodec^=mp4a]/"
            f"bv*{lim}+ba/b{lim}/bv*+ba/b")


def build_download_opts(kind: str, quality: str, start: float, end: float,
                        out_dir: str, basename: str, temp_dir: str,
                        cookies_browser: str = "None") -> dict:
    from yt_dlp.utils import download_range_func
    opts = _base_opts(cookies_browser)
    opts.update({
        "format": format_selector(kind, quality),
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,   # re-encode around cut points -> accurate start/end
        "paths": {"home": out_dir, "temp": temp_dir},
        "outtmpl": {"default": basename.replace("%", "%%") + ".%(ext)s"},
        "overwrites": True,
        "postprocessors": [],
    })
    if kind == "MP3":
        opts["postprocessors"].append({"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3",
                                       "preferredquality": str(quality)})
    else:
        opts["merge_output_format"] = "mp4"
        opts["format_sort"] = ["vcodec:h264", "acodec:aac", "res", "fps"]
    opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})
    return opts


def unique_basename(out_dir: str, title: str, start: float, end: float, ext: str) -> str:
    base = f"{sanitize_filename(title)}_{_stamp(start)}-{_stamp(end)}"
    cand, n = base, 2
    while os.path.exists(os.path.join(out_dir, f"{cand}.{ext}")):
        cand = f"{base} ({n})"
        n += 1
    return cand


def ffprobe_streams(path: str) -> dict:
    """{'duration': float, 'vcodec': str|None, 'acodec': str|None}"""
    ffprobe = shutil.which("ffprobe") or os.path.join(os.path.dirname(find_ffmpeg() or ""), "ffprobe")
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,codec_name", "-of", "json", path],
                       capture_output=True, text=True, timeout=60)
    data = json.loads(r.stdout or "{}")
    out = {"duration": float(data.get("format", {}).get("duration", 0) or 0),
           "vcodec": None, "acodec": None}
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not out["vcodec"]:
            out["vcodec"] = s.get("codec_name")
        if s.get("codec_type") == "audio" and not out["acodec"]:
            out["acodec"] = s.get("codec_name")
    return out


# --------------------------------------------------------------------------- #
# Worker (runs in a child process)
# --------------------------------------------------------------------------- #
def _emit(**kw):
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def _worker_main(job: dict) -> int:
    import yt_dlp

    def pp_hook(d):
        if d.get("status") == "started":
            name = str(d.get("postprocessor") or "")
            text = ("Converting to MP3…" if "ExtractAudio" in name else
                    "Joining video and audio…" if "Merge" in name else
                    "Adding title info…" if "Metadata" in name else "Finishing up…")
            _emit(event="stage", phase="post", text=text)

    def dl_hook(d):
        if d.get("status") == "downloading":
            _emit(event="bytes", done=d.get("downloaded_bytes") or 0,
                  total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        elif d.get("status") == "finished":
            _emit(event="stage", phase="download", text="Part downloaded…")

    def find_output(info):
        if info and info.get("_type") == "playlist":
            info = next((e for e in info.get("entries") or [] if e), info)
        path = None
        for rd in (info or {}).get("requested_downloads") or []:
            path = rd.get("filepath") or path
        return path if path and os.path.exists(path) else None

    # 1) Fast path: ask YouTube for just the section (yt-dlp + ffmpeg).
    opts = build_download_opts(job["kind"], job["quality"], job["start"], job["end"],
                               job["out_dir"], job["basename"], job["temp_dir"],
                               job.get("cookies_browser", "None"))
    opts["progress_hooks"] = [dl_hook]
    opts["postprocessor_hooks"] = [pp_hook]
    def try_section() -> Optional[str]:
        """Returns None on success (after emitting the path), else an error text."""
        if os.environ.get("CLIPMAKER_FORCE_FALLBACK"):     # used by tests
            return "ffmpeg exited with code 8 (forced for testing)"
        try:
            _emit(event="stage", phase="download", text="Downloading the section…")
            with yt_dlp.YoutubeDL(opts) as ydl:
                path = find_output(_process(ydl, job))
            if path:
                _emit(event="downloaded", path=path)
                return None
            return "Download finished but the file wasn't found."
        except Exception as e:
            return str(e)

    first_error = try_section()
    if first_error is None:
        return 0
    if os.environ.get("CLIPMAKER_DEBUG"):
        sys.stderr.write(f"[section download failed] {first_error}\n")

    # 2) Fallback: YouTube sometimes refuses ffmpeg's direct section requests
    #    (HTTP 403). Then download the whole stream with yt-dlp's own downloader
    #    and cut it locally. Slower for long videos, but works.
    low = first_error.lower()
    if not ("ffmpeg exited" in low or "403" in low or "forbidden" in low
            or "file wasn't found" in low):
        _emit(event="error", text=friendly_error(first_error))
        return 1
    _emit(event="fallback", text="Quick section download was refused; "
                                 "downloading the full video, then cutting (slower)…")
    for f in os.listdir(job["temp_dir"]):
        fp = os.path.join(job["temp_dir"], f)
        if os.path.isdir(fp):
            shutil.rmtree(fp, ignore_errors=True)
        else:
            os.remove(fp)
    try:
        src = _download_full(job, dl_hook)
        out = _cut_locally(job, src)
    except Exception as e:
        if os.environ.get("CLIPMAKER_DEBUG"):
            sys.stderr.write(f"[fallback failed] {e}\n")
        _emit(event="error", text=friendly_error(str(e)))
        return 1
    _emit(event="downloaded", path=out)
    return 0


def _process(ydl, job: dict) -> dict:
    # Note: we deliberately look the video up again here instead of reusing the
    # info from "Get video": YouTube's stream links are tied to the session that
    # fetched them, and reusing them from another process gets HTTP 403.
    return ydl.extract_info(job["url"], download=True)


def _download_full(job: dict, dl_hook) -> str:
    import yt_dlp
    opts = _base_opts(job.get("cookies_browser", "None"))
    opts.update({
        "format": format_selector(job["kind"], job["quality"]),
        "paths": {"home": job["temp_dir"], "temp": job["temp_dir"]},
        "outtmpl": {"default": "source.%(ext)s"},
        "progress_hooks": [dl_hook],
        "overwrites": True,
    })
    if job["kind"] != "MP3":
        opts["merge_output_format"] = "mkv"     # holds any codec; we re-encode anyway
        opts["format_sort"] = ["vcodec:h264", "acodec:aac", "res", "fps"]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = _process(ydl, job)
    for rd in (info or {}).get("requested_downloads") or []:
        if rd.get("filepath") and os.path.exists(rd["filepath"]):
            return rd["filepath"]
    files = [os.path.join(job["temp_dir"], f) for f in os.listdir(job["temp_dir"])
             if f.startswith("source.") and not f.endswith(".part")]
    if not files:
        raise RuntimeError("Download finished but the file wasn't found.")
    return files[0]


def _cut_locally(job: dict, src: str) -> str:
    """Accurate cut + encode with ffmpeg. Emits progress events."""
    start, end = float(job["start"]), float(job["end"])
    length = end - start
    ext = "mp3" if job["kind"] == "MP3" else "mp4"
    out = os.path.join(job["out_dir"], f"{job['basename']}.{ext}")
    cmd = [find_ffmpeg(), "-y", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-i", src,
           "-t", f"{length:.3f}", "-map_metadata", "-1",
           "-metadata", f"title={job.get('title', '')}"]
    if job["kind"] == "MP3":
        cmd += ["-vn", "-c:a", "libmp3lame", "-b:a", f"{job['quality']}k"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", out]
    _emit(event="stage", phase="post", text="Cutting the clip…")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in p.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                done = int(line.split("=")[1]) / 1e6
                _emit(event="cut", frac=max(0.0, min(1.0, done / length)))
            except ValueError:
                pass
    p.wait()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg couldn't cut the clip: " + p.stderr.read()[-300:])
    return out


# --------------------------------------------------------------------------- #
# ClipJob: what the GUI (and tests) use
# --------------------------------------------------------------------------- #
ProgressCB = Callable[[Optional[float], str], None]   # (fraction or None, status text)


class ClipJob:
    def __init__(self, info: VideoInfo, start: float, end: float, kind: str, quality: str,
                 out_dir: str, cookies_browser: str = "None",
                 on_progress: Optional[ProgressCB] = None):
        self.info, self.start, self.end = info, float(start), float(end)
        self.kind, self.quality = kind.upper(), quality
        self.out_dir, self.cookies_browser = out_dir, cookies_browser
        self.on_progress = on_progress or (lambda f, s: None)
        self._cancel = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

    # -- control ---------------------------------------------------------- #
    def cancel(self):
        self._cancel.set()
        self._kill()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _kill(self):
        p = self._proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)   # kills yt-dlp AND its ffmpeg children
            except Exception:
                p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    p.kill()

    def _check(self):
        if self._cancel.is_set():
            raise Cancelled()

    # -- estimate size for a progress bar --------------------------------- #
    def _estimated_bytes(self) -> int:
        fmts = self.info.raw_formats or []
        frac = (self.end - self.start) / self.info.duration if self.info.duration else None
        if not frac:
            return 0

        def size(f):
            return f.get("filesize") or f.get("filesize_approx") or 0
        try:
            if self.kind == "MP3":
                aud = [f for f in fmts if f.get("vcodec") == "none" and f.get("ext") == "m4a"]
                best = max(aud, key=lambda f: f.get("abr") or 0, default=None)
                return int(size(best) * frac) if best else 0
            h = {"1080p": 1080, "720p": 720, "480p": 480}.get(self.quality, 10 ** 5)
            vids = [f for f in fmts if str(f.get("vcodec", "")).startswith("avc1")
                    and (f.get("height") or 0) <= h and f.get("acodec") == "none"]
            v = max(vids, key=lambda f: f.get("height") or 0, default=None)
            aud = [f for f in fmts if f.get("vcodec") == "none" and f.get("ext") == "m4a"]
            a = max(aud, key=lambda f: f.get("abr") or 0, default=None)
            return int((size(v) + size(a)) * frac) if v else 0
        except Exception:
            return 0

    # -- run -------------------------------------------------------------- #
    def run(self) -> str:
        """Blocking. Returns the final file path. Raises ClipError / Cancelled."""
        validate_range(self.start, self.end, self.info.duration)
        if not find_ffmpeg():
            raise ClipError("ffmpeg is missing. Open Terminal and run:  brew install ffmpeg")
        if not os.path.isdir(self.out_dir):
            raise ClipError(f"Output folder doesn't exist:\n{self.out_dir}")
        if not os.access(self.out_dir, os.W_OK):
            raise ClipError(f"Can't write to the output folder:\n{self.out_dir}")

        ext = "mp3" if self.kind == "MP3" else "mp4"
        basename = unique_basename(self.out_dir, self.info.title, self.start, self.end, ext)
        temp_dir = tempfile.mkdtemp(prefix="clipmaker-")
        job = {"url": self.info.url, "kind": self.kind, "quality": self.quality,
               "start": self.start, "end": self.end, "out_dir": self.out_dir,
               "basename": basename, "temp_dir": temp_dir,
               "cookies_browser": self.cookies_browser, "title": self.info.title}
        est = self._estimated_bytes()
        result = {"path": None, "error": None}
        try:
            self.on_progress(0.02, "Starting…")
            self._check()
            self._proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--worker", json.dumps(job)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True, env=os.environ.copy())

            stop_watch = threading.Event()
            fallback = False

            def watch_temp():   # ffmpeg section downloads don't report bytes; watch the disk
                t0 = time.time()
                while not stop_watch.wait(0.5):
                    if fallback:
                        continue
                    got = 0
                    cands = [os.path.join(r, f) for r, _, fs in os.walk(temp_dir) for f in fs]
                    try:
                        cands += [os.path.join(self.out_dir, f) for f in os.listdir(self.out_dir)
                                  if f.startswith(basename)]
                    except OSError:
                        pass
                    for fp in cands:
                        try:
                            got += os.path.getsize(fp)
                        except OSError:
                            pass
                    secs = int(time.time() - t0)
                    if est > 0:
                        self.on_progress(min(0.9, 0.05 + 0.85 * got / est),
                                         f"Downloading the section… {got / 1e6:.1f} MB")
                    elif got:
                        self.on_progress(None, f"Downloading the section… {got / 1e6:.1f} MB ({secs}s)")
            threading.Thread(target=watch_temp, daemon=True).start()

            stderr_chunks: list[str] = []
            threading.Thread(target=lambda: stderr_chunks.append(self._proc.stderr.read()),
                             daemon=True).start()
            for line in self._proc.stdout:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("event") == "stage":
                    if ev.get("phase") == "post":
                        stop_watch.set()
                        self.on_progress(0.92, ev["text"])
                    elif est <= 0:
                        self.on_progress(None, ev["text"])
                elif ev.get("event") == "fallback":
                    fallback = True
                    self.on_progress(0.05, ev["text"])
                elif ev.get("event") == "bytes" and fallback:
                    if ev.get("total"):
                        self.on_progress(0.05 + 0.75 * ev["done"] / ev["total"],
                                         f"Downloading full video… {ev['done'] / 1e6:.1f} of "
                                         f"{ev['total'] / 1e6:.1f} MB")
                elif ev.get("event") == "cut":
                    self.on_progress(0.8 + 0.18 * ev["frac"], "Cutting the clip…")
                elif ev.get("event") == "downloaded":
                    result["path"] = ev["path"]
                elif ev.get("event") == "error":
                    result["error"] = ev["text"]
            self._proc.wait()
            stop_watch.set()
            if os.environ.get("CLIPMAKER_DEBUG"):
                time.sleep(0.2)
                sys.stderr.write("".join(stderr_chunks)[-3000:])
            self._check()
            if result["error"]:
                raise ClipError(result["error"])
            if not result["path"]:
                err = "".join(stderr_chunks).strip()
                raise ClipError(friendly_error(err) if err else "The download stopped unexpectedly.")

            path = result["path"]
            if self.kind == "MP4":
                path = self._ensure_quicktime_compatible(path)
            self.on_progress(1.0, "Done!")
            return path
        except Cancelled:
            self._kill()
            # remove any half-written output
            for e in ("mp3", "mp4", "m4a", "webm", "part"):
                p = os.path.join(self.out_dir, f"{basename}.{e}")
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _ensure_quicktime_compatible(self, path: str) -> str:
        """Re-encode to H.264/AAC MP4 only if YouTube gave us VP9/AV1/Opus."""
        try:
            s = ffprobe_streams(path)
        except Exception:
            return path
        if s["vcodec"] in ("h264", None) and s["acodec"] in ("aac", None) and path.endswith(".mp4"):
            return path
        self.on_progress(0.94, "Converting to a QuickTime-friendly MP4…")
        out = os.path.splitext(path)[0] + ".tmp-h264.mp4"
        cmd = [find_ffmpeg(), "-y", "-v", "error", "-i", path,
               "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
        self._proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, start_new_session=True)
        _, err = self._proc.communicate()
        self._check()
        if self._proc.returncode != 0:
            raise ClipError("Couldn't convert the video: " + err.decode(errors="ignore")[-200:])
        final = os.path.splitext(path)[0] + ".mp4"
        os.remove(path)
        os.replace(out, final)
        return final


def reveal_in_finder(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--worker":
    sys.exit(_worker_main(json.loads(sys.argv[2])))
