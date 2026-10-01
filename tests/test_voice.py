import json
import subprocess

import pytest

from autocutlib import voice
from autocutlib.voice import batches, convert_batch, merge_ranges
from conftest import needs_ffmpeg


def test_merge_ranges_pads_joins_and_clamps():
    assert merge_ranges([(5.0, 6.0), (1.0, 2.0), (2.5, 3.0), (9.8, 10.5)], 10.0) == [
        (0.6, 3.4), (4.6, 6.4), (9.4, 10.0)]


def silent_fal(calls):
    """Stand-in for the fal API: returns silence as long as the input, plus a bit (to test trimming)."""
    def fake(src, voice_name, denoise, rep, seed):
        calls.append((voice_name, seed))
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
    # CLAIRE's two stretches (~4.5s and ~13.5s) go in ONE request, with the fixed seed.
    assert fake_fal == [("Aria", 42)]

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
    assert fake_fal == [("Aria", 42)]
    # A different seed is a different rendition: sent again.
    opts.voice_seed = 7
    run(project / "scene.pdf",
        [("MIKEY", project / "mikey.mov"), ("CLAIRE", project / "claire.mp4")], out, None, opts)
    assert fake_fal == [("Aria", 42), ("Aria", 7)]


@needs_ffmpeg
def test_all_takes_of_a_character_share_one_request(project, tmp_path, fake_fal):
    from autocutlib.pipeline import Options, run
    opts = Options(convert_dir=str(tmp_path / "conv"), voices={"MIKEY": "Brian"})
    run(project / "scene.pdf", [("MIKEY", project / "mikey.mov"), ("CLAIRE", project / "claire.mp4"),
                                ("MIKEY", project / "mikey2.mpeg")], tmp_path / "cut.fcpxml", None, opts)
    assert fake_fal == [("Brian", 42)]
    assert (tmp_path / "conv" / "mikey.Brian.mov").exists()
    assert (tmp_path / "conv" / "mikey2.prores.Brian.mov").exists()


def test_batches_split_long_requests():
    items = [("a", 0, 100), ("a", 200, 300), ("b", 0, 100)]
    assert batches(items, limit=250) == [items[:2], items[2:]]
    assert batches(items, limit=1000) == [items]


@needs_ffmpeg
def test_split_back_stays_in_sync_when_api_stretches(tmp_path, monkeypatch):
    """Beeps at known times survive the round trip at the right place, even if the API's
    output is 3% longer than its input."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(voice.sys, "platform", "linux")
    src = tmp_path / "beeps.wav"     # 1s beep at 2s and at 12s, silence elsewhere
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=500:duration=20:sample_rate=48000",
                    "-af", "volume='if(between(t,2,3)+between(t,12,13),1,0)':eval=frame", str(src)], check=True)

    def stretching(path, voice_name, denoise, rep, seed):
        out = path.with_name("stretched.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-af", "atempo=0.97",
                        str(out)], check=True)
        return out.as_uri()

    (a, b), sent = convert_batch([(src, 1.0, 4.0), (src, 11.0, 14.5)], "X", False, 42, voice.Reporter(),
                                 caller=stretching)
    assert sent > 6
    assert max_volume(a, 1.15, 0.7) > -25          # beep at 1-2s into the first stretch
    assert max_volume(a, 0.0, 0.8) < -50
    assert max_volume(a, 2.2, 0.7) < -50
    assert max_volume(b, 1.15, 0.7) > -25          # and 1-2s into the second
    assert max_volume(b, 2.2, 1.2) < -50
    # Onset is where it should be (without re-timing it would land ~0.1s late in the second one).
    assert max_volume(b, 0.85, 0.12) < -50
    assert max_volume(b, 1.02, 0.07) > -25


def test_voice_needs_key(monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY_ID", raising=False)
    from autocutlib.report import AutocutError
    with pytest.raises(AutocutError, match="fal.ai API key"):
        voice.require_fal()
