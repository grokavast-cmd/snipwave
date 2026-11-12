"""Headless tests. Run:  .venv/bin/python -m tests.test_core [--online]"""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

def test_helpers():
    assert core.parse_time("1:02:03") == 3723
    assert core.parse_time("2:05") == 125
    assert core.parse_time("45") == 45
    for bad in ("", "1:99", "a:b", "1:2:3:4"):
        try:
            core.parse_time(bad); raise AssertionError(bad)
        except ValueError:
            pass
    assert core.format_time(3723) == "01:02:03"
    assert core.sanitize_filename('a/b:c?"d') == "a b c d"
    assert core._stamp(65) == "1m05s" and core._stamp(3725) == "1h02m05s"
    for s, e in ((5, 5), (5, 3), (0, 100)):
        try:
            core.validate_range(s, e, 19); raise AssertionError((s, e))
        except core.ClipError:
            pass
    core.validate_range(3, 12, 19)
    assert "private" in core.friendly_error("ERROR: [youtube] x: Private video. Sign in").lower()
    assert "age-restricted" in core.friendly_error("Sign in to confirm your age")
    assert "Network" in core.friendly_error("Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided")
    assert "YouTube video link" in core.friendly_error("Unsupported URL: https://example.com")
    print("helpers OK")

def run_clip(url, kind, quality, start, end, out):
    info = core.fetch_info(url)
    print(f"info: {info.title!r} duration={info.duration}")
    job = core.ClipJob(info, start, end, kind, quality, out,
                       on_progress=lambda f, s: print(f"  [{'--' if f is None else f'{f:.2f}'}] {s}"))
    path = job.run()
    st = core.ffprobe_streams(path)
    print(f"RESULT {kind}: {os.path.basename(path)} -> {st}")
    return path, st

def test_local_cut():
    """Offline: exercise the ffmpeg cut/encode path on a generated 20 s video."""
    d = tempfile.mkdtemp(prefix="clipmaker-local-")
    src = os.path.join(d, "source.mkv")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "20",
                    "-c:v", "libvpx-vp9", "-b:v", "300k", "-c:a", "libopus", src], check=True)
    core._emit = lambda **kw: None
    for kind, q in (("MP3", "192"), ("MP4", "720p")):
        job = {"kind": kind, "quality": q, "start": 4.0, "end": 14.0, "out_dir": d,
               "basename": f"local_{kind}", "title": "Local test"}
        out = core._cut_locally(job, src)
        st = core.ffprobe_streams(out)
        print(f"LOCAL {kind}: {os.path.basename(out)} -> {st}")
        assert abs(st["duration"] - 10) < 0.2, st
        assert (st["acodec"] == "mp3") if kind == "MP3" else (st["vcodec"] == "h264" and st["acodec"] == "aac")
    print("local cut OK")

if __name__ == "__main__":
    test_helpers()
    test_local_cut()
    if "--online" in sys.argv:
        url = os.environ.get("TEST_URL", "https://www.youtube.com/watch?v=jNQXAC9IVRw")
        out = tempfile.mkdtemp(prefix="clipmaker-test-")
        p, st = run_clip(url, "MP3", "192", 3, 13, out)
        assert st["acodec"] == "mp3" and abs(st["duration"] - 10) < 1.0, st
        p, st = run_clip(url, "MP4", "1080p", 3, 13, out)
        assert st["vcodec"] == "h264" and st["acodec"] == "aac" and abs(st["duration"] - 10) < 1.0, st
        if "--fallback" in sys.argv:   # also exercise the full-download + local-cut path
            os.environ["CLIPMAKER_FORCE_FALLBACK"] = "1"
            p, st = run_clip(url, "MP3", "320", 2, 9.5, out)
            assert st["acodec"] == "mp3" and abs(st["duration"] - 7.5) < 1.0, st
            p, st = run_clip(url, "MP4", "720p", 2, 9.5, out)
            assert st["vcodec"] == "h264" and st["acodec"] == "aac" and abs(st["duration"] - 7.5) < 1.0, st
        print("online OK ->", out)
