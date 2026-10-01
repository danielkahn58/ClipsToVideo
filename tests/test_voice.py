import json
import subprocess

import pytest

from autocutlib import voice
from autocutlib.voice import merge_ranges
from conftest import needs_ffmpeg


def test_merge_ranges_pads_joins_and_clamps():
    assert merge_ranges([(5.0, 6.0), (1.0, 2.0), (2.5, 3.0), (9.8, 10.5)], 10.0) == [
        (0.6, 3.4), (4.6, 6.4), (9.4, 10.0)]


def silent_fal(calls):
    """Stand-in for the fal API: returns silence as long as the input, plus a bit (to test trimming)."""
    def fake(src, voice_name, denoise, rep):
        calls.append(voice_name)
        out = src.with_name(src.stem + ".fake.mp3")
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(src)], capture_output=True, text=True).stdout)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=mono", "-t", f"{dur + 0.2:.3f}", str(out)], check=True)
        return out.as_uri()
    return fake


def max_volume(path, start, dur):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(dur), "-i", str(path),
                        "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    line = next(ln for ln in r.stderr.splitlines() if "max_volume" in ln)
    return float(line.split(":")[1].split()[0])


@pytest.fixture
def fake_fal(monkeypatch, tmp_path):
    monkeypatch.setenv("FAL_KEY", "test")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(voice.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(voice, "call_fal", silent_fal(calls))
    return calls


@needs_ffmpeg
def test_voice_changed_take(project, tmp_path, fake_fal):
    from autocutlib.pipeline import Options, run
    out = tmp_path / "cut.fcpxml"
    opts = Options(convert_dir=str(tmp_path / "conv"), voices={"claire": "Aria"})
    res = run(project / "scene.pdf",
              [("MIKEY", project / "mikey.mov"), ("CLAIRE", project / "claire.mp4")], out, None, opts)

    voiced = tmp_path / "conv" / "claire.Aria.mov"
    assert voiced.exists()
    assert voiced.resolve().as_uri() in out.read_text()
    claire = next(t for t in res["takes"] if t["character"] == "CLAIRE")
    assert claire["voice"] == "Aria" and claire["path"] == str(voiced)
    # CLAIRE's lines 2 and 5 are at ~4.5s and ~13.5s and were merged into separate stretches.
    assert fake_fal == ["Aria", "Aria"]

    # Video is copied untouched; audio is silent (converted) inside her lines, original tone elsewhere.
    streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(voiced)],
                                        capture_output=True, text=True).stdout)["streams"]
    assert [s["codec_name"] for s in streams if s["codec_type"] == "video"] == ["h264"]
    assert max_volume(voiced, 5.0, 1.0) < -80
    assert max_volume(voiced, 13.8, 1.0) < -80
    assert max_volume(voiced, 10.5, 1.0) > -30

    # Second run: converted stretches and the voiced take are reused, nothing re-sent.
    run(project / "scene.pdf",
        [("MIKEY", project / "mikey.mov"), ("CLAIRE", project / "claire.mp4")], out, None, opts)
    assert fake_fal == ["Aria", "Aria"]


def test_voice_needs_key(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY_ID", raising=False)
    from autocutlib.report import AutocutError
    with pytest.raises(AutocutError, match="fal.ai API key"):
        voice.require_fal()
