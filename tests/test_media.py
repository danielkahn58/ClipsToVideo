import json

from autocutlib.media import converted_path, resolve_problems
from autocutlib.transcribe import Transcriber, cache_path
from conftest import needs_ffmpeg


def info(container, v, a):
    return {"container": container, "vcodec": v, "acodec": a}


def test_resolve_problems():
    assert resolve_problems(info(".mov", "h264", "aac")) == []
    assert resolve_problems(info(".mxf", "dnxhd", "pcm_s24le")) == []
    assert resolve_problems(info(".mp4", "hevc", None)) == []
    assert resolve_problems(info(".mpeg", "mpeg2video", "mp2")) == [
        ".mpeg container", "mpeg2video video", "mp2 audio"]
    assert resolve_problems(info(".mov", "h264", "mp3")) == ["mp3 audio"]


def test_converted_path(tmp_path):
    assert converted_path("/a/b/take.mpeg").as_posix() == "/a/b/take.prores.mov"
    assert converted_path("/a/b/take.mpeg", tmp_path) == tmp_path / "take.prores.mov"


def test_transcript_cache_shared_with_original(tmp_path):
    src, conv = tmp_path / "take.mpeg", tmp_path / "take.prores.mov"
    words = [{"word": "hi", "start": 0.1, "end": 0.4}]
    cache_path(src).write_text(json.dumps(words))
    got = Transcriber().words(conv, aliases=[src])      # no WhisperX needed: cache hit
    assert got == words
    assert json.loads(cache_path(conv).read_text()) == words


@needs_ffmpeg
def test_pipeline_end_to_end(project, tmp_path):
    from autocutlib.pipeline import Options, run
    out, prev = tmp_path / "cut.fcpxml", tmp_path / "cut.mp4"
    res = run(project / "scene.pdf",
              [("MIKEY", project / "mikey.mov"), ("CLAIRE", project / "claire.mp4"),
               ("mikey", project / "mikey2.mpeg")],
              out, prev, Options(convert_dir=str(tmp_path / "conv")))
    conv = tmp_path / "conv" / "mikey2.prores.mov"
    assert conv.exists() and cache_path(conv).exists()
    xml = out.read_text()
    assert conv.resolve().as_uri() in xml and "mikey2.mpeg" not in xml
    assert prev.stat().st_size > 0
    assert [s["character"] for s in res["segments"]] == ["MIKEY", "CLAIRE", "MIKEY", "CLAIRE", "MIKEY"]
    assert res["segments"][0]["alts"][0]["take"] == 2
    assert res["takes"][1]["converted"] is True
