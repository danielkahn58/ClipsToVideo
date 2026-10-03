"""Projects shared between two computers through a store (a LocalStore standing in for Drive)."""

import json
import shutil

import pytest

from autocutlib import voice
from autocutlib.cloud import LocalStore
from autocutlib.projects import Conflict, Project, create_project, list_projects
from autocutlib.report import Reporter
from autocutlib.transcribe import cache_path
from autocutlib.web import worker
from conftest import needs_ffmpeg
from test_voice import silent_fal


@pytest.fixture
def drive(tmp_path):
    return LocalStore(tmp_path / "drive")


def test_state_roundtrip_and_conflict(drive, tmp_path):
    a = create_project(drive, "Scene 4", tmp_path / "A")
    assert [p["name"] for p in list_projects(drive)] == ["Scene 4"]
    state, mod = a.load_state()
    state["pages"] = "3-4"
    mod_a = a.save_state(state, mod)

    b = Project.open(drive, a.id, tmp_path / "B")
    state_b, mod_b = b.load_state()
    assert state_b["pages"] == "3-4" and mod_b == mod_a
    state_b["pages"] = "5"
    b.save_state(state_b, mod_b)

    with pytest.raises(Conflict):            # A still has the old version
        a.save_state(state, mod_a)


def test_duplicate_project_name(drive, tmp_path):
    create_project(drive, "X", tmp_path / "A")
    with pytest.raises(Exception, match="already exists"):
        create_project(drive, "X", tmp_path / "A")


@needs_ffmpeg
def test_two_computers(project, drive, tmp_path, monkeypatch):
    """A uploads and cuts (with a voice); B opens the project, cuts without transcribing, and
    exports an FCPXML that points at B's own files without paying for the voice again."""
    src = tmp_path / "originals"
    shutil.copytree(project, src)
    calls = []
    monkeypatch.setattr(voice, "call_fal", silent_fal(calls))
    monkeypatch.setenv("FAL_KEY", "test")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("AUTOCUT_STORE", f"local:{drive.root}")
    rep = Reporter()

    # ---- computer A
    home_a = tmp_path / "A"
    a = create_project(drive, "Kitchen", home_a)
    script = a.upload_file(src / "scene.pdf", rep)
    mikey = a.upload_file(src / "mikey.mov", rep)
    claire = a.upload_file(src / "claire.mp4", rep)
    assert a.local_path(mikey) == src / "mikey.mov"                  # used in place, not copied
    assert drive.find(a.sub("transcripts"), "mikey.mov.words.json")   # transcript came along

    def cut(home, job):
        job_dir = tmp_path / job
        job_dir.mkdir()
        cfg = {"kind": "cut", "home": str(home), "project": {"id": a.id}, "name": "cut",
               "script_ref": script, "take_refs": [["MIKEY", mikey], ["CLAIRE", claire]],
               "options": {"voices": {"CLAIRE": "Aria"}, "convert_dir": None},
               "out": str(job_dir / "cut.fcpxml"), "preview": None}
        return worker.run_cut(cfg, job_dir, rep)

    res_a = cut(home_a, "job-a")
    assert calls == [("Aria", 42)]
    assert [r["id"] for r in a.list_runs()] == ["job-a"]
    assert all(t["ref"] for t in res_a["takes"])

    # ---- computer B: no local files, no transcripts, no voice cache
    home_b = tmp_path / "B"
    b = Project.open(drive, a.id, home_b)
    for f in src.glob("*.words.json"):
        f.unlink()                                  # prove B gets transcripts from Drive
    res_b = cut(home_b, "job-b")                   # would need WhisperX if transcripts didn't sync
    assert calls == [("Aria", 42)]                  # voice audio reused, not re-sent
    def no_paths(segs):
        return [{**s, "primary": {k: v for k, v in s["primary"].items() if k != "file"},
                 "alts": [{k: v for k, v in x.items() if k != "file"} for x in s["alts"]]} for s in segs]
    assert no_paths(res_b["segments"]) == no_paths(res_a["segments"])
    assert str(b.local) in res_b["takes"][0]["source"]

    # B exports A's run for itself.
    out = tmp_path / "export" / "cut.fcpxml"
    worker.run_export({"home": str(home_b), "project": {"id": a.id}, "run_id": "job-a",
                       "out": str(out), "converted_dir": str(home_b / "converted")}, tmp_path, rep)
    xml = out.read_text()
    assert str(src) not in xml                      # no paths from computer A
    assert (b.local / "files").resolve().as_uri() in xml
    assert "claire.Aria.mov" in xml                 # voice re-applied from synced audio
    assert calls == [("Aria", 42)]

    # The run's files are in Drive for every computer.
    runs = {r["id"]: r for r in b.list_runs()}
    assert set(runs) == {"job-a", "job-b"} and runs["job-a"]["clips"] == len(res_a["segments"])
    assert json.loads(drive.read_bytes(drive.find(b.run_folder("job-a"), "result.json")["id"]))


def test_ensure_local_downloads_once(drive, tmp_path, project):
    a = create_project(drive, "P", tmp_path / "A")
    ref = a.upload_file(project / "scene.pdf")
    b = Project.open(drive, a.id, tmp_path / "B")
    p1 = b.ensure_local(ref)
    mtime = p1.stat().st_mtime_ns
    p2 = b.ensure_local(ref)
    assert p1 == p2 and p2.stat().st_mtime_ns == mtime
    assert not cache_path(p1).exists()
