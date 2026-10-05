import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from autocutlib.align import align
from autocutlib.edit import Take, build_segments
from autocutlib.fcpxml import write_fcpxml
from autocutlib.pipeline import Options
from autocutlib.screenplay import Line
from autocutlib.text import tokenize
from autocutlib.transcribe import transcript_tokens
from fixtures import DIALOGUE, words_for

INFO = {"width": 1920, "height": 1080, "rate": Fraction(24), "fd": Fraction(1, 24),
        "timecode": None, "duration": 30.0, "channels": 2, "sample_rate": 48000}


def lines():
    out = []
    for who, text in DIALOGUE:
        out.append(Line(who, text, 1, tokenize(text)))
    return out


def setup(take_timings, words_override=None):
    """take_timings: {(char, index): line start times}. Returns takes, transcripts, spans."""
    ls = lines()
    takes, transcripts, spans = {}, {}, {}
    for (char, idx), timings in take_timings.items():
        t = Take(char, idx, Path(f"/media/{char.lower()}{idx + 1}.mov"),
                 Path(f"/media/{char.lower()}{idx + 1}.mov"), dict(INFO))
        takes.setdefault(char, []).append(t)
        words = (words_override or {}).get(t.key) or words_for(timings)
        transcripts[t.key] = transcript_tokens(words)
        spans[t.key] = align(ls, transcripts[t.key])
    return ls, takes, transcripts, spans


BASIC = {("MIKEY", 0): [1, 4, 7, 9, 13, 16], ("CLAIRE", 0): [1, 4, 7, 9, 13, 16]}


def test_merges_same_speaker_runs():
    ls, takes, tr, sp = setup(BASIC)
    segs = build_segments(ls, takes, tr, sp, Options())
    assert [s.lines for s in segs] == [[1], [2], [3, 4], [5], [6]]
    assert [s.character for s in segs] == ["MIKEY", "CLAIRE", "MIKEY", "CLAIRE", "MIKEY"]
    assert segs[2].text == "Then who finished it? / It was full this morning."


def test_no_merge_and_merge_gap():
    ls, takes, tr, sp = setup(BASIC)
    assert len(build_segments(ls, takes, tr, sp, Options(no_merge=True))) == 6
    # Lines 3 and 4 are ~0.6s apart (with padding), so a tiny merge gap splits them.
    assert len(build_segments(ls, takes, tr, sp, Options(merge_gap=0.1))) == 6


def test_alternates_follow_main_take():
    ls, takes, tr, sp = setup({**BASIC, ("MIKEY", 1): [2, 5, 8, 10, 14, 17]})
    segs = build_segments(ls, takes, tr, sp, Options())
    mikey = [s for s in segs if s.character == "MIKEY"]
    assert all(s.primary.take.index == 0 for s in mikey)
    assert all([a.take.index for a in s.alts] == [1] for s in mikey)
    assert mikey[0].alts[0].start > mikey[0].primary.start   # alt timing comes from its own take


def test_line_missing_from_main_take_uses_alternate():
    # Main MIKEY take never says line 6 (mumbled): replace its words with noise.
    words = words_for([1, 4, 7, 9, 13, 16])
    for w in words[-5:]:
        w["word"] = "mmm"
    ls, takes, tr, sp = setup({**BASIC, ("MIKEY", 1): [2, 5, 8, 10, 14, 17]},
                              {("MIKEY", 0): words})
    segs = build_segments(ls, takes, tr, sp, Options())
    assert segs[-1].lines == [6]
    assert segs[-1].primary.take.index == 1
    assert segs[-1].alts == []


def test_pick_best_prefers_better_match():
    words = words_for([1, 4, 7, 9, 13, 16])
    words[0]["word"] = "dud"          # main take fluffs the first word of line 1
    timings = {**BASIC, ("MIKEY", 1): [2, 5, 8, 10, 14, 17]}
    ls, takes, tr, sp = setup(timings, {("MIKEY", 0): words})
    plain = build_segments(ls, takes, tr, sp, Options())
    best = build_segments(ls, takes, tr, sp, Options(pick_best=True))
    assert plain[0].primary.take.index == 0 and plain[0].ratio < 1
    assert best[0].primary.take.index == 1 and best[0].ratio == 1
    assert [a.take.index for a in best[0].alts] == [0]
    assert best[2].primary.take.index == 0          # tie -> main take


def test_unfound_line_is_skipped():
    words = words_for([1, 4, 7, 9, 13, 16])
    for w in words[8:14]:             # CLAIRE's line 2, in CLAIRE's take
        w["word"] = "zzz"
    ls, takes, tr, sp = setup(BASIC, {("CLAIRE", 0): words})
    segs = build_segments(ls, takes, tr, sp, Options())
    assert 2 not in [n for s in segs for n in s.lines]


def fcp(tmp_path, enable_alts, layout="stacked", opts=None, words=None):
    ls, takes, tr, sp = setup({**BASIC, ("MIKEY", 1): [2, 5, 8, 10, 14, 17]}, words)
    segs = build_segments(ls, takes, tr, sp, opts or Options())
    all_takes = [t for ts in takes.values() for t in ts]
    out = tmp_path / "cut.fcpxml"
    write_fcpxml(segs, all_takes, out, "cut", enable_alts, layout=layout)
    return segs, ET.parse(out).getroot()


def secs(t):
    return Fraction(t.rstrip("s"))


def test_fcpxml_spine_and_stacked_alts(tmp_path):
    segs, root = fcp(tmp_path, enable_alts=False)
    spine = root.find(".//spine")
    clips = list(spine)
    assert len(clips) == len(segs)
    # Contiguous on the timeline.
    pos = Fraction(0)
    for c in clips:
        assert secs(c.get("offset")) == pos
        pos += secs(c.get("duration"))
    assert secs(root.find(".//sequence").get("duration")) == pos
    # MIKEY's clips carry take 2 on lane 1, disabled, anchored at the parent's start.
    alts = clips[0].findall("asset-clip")
    assert len(alts) == 1
    assert alts[0].get("lane") == "1" and alts[0].get("enabled") == "0"
    assert alts[0].get("offset") == clips[0].get("start")
    assert clips[1].findall("asset-clip") == []
    srcs = {a.get("src") for a in root.iter("asset")}
    assert "file:///media/mikey2.mov" in srcs


def test_fcpxml_enable_alts(tmp_path):
    _, root = fcp(tmp_path, enable_alts=True)
    alts = root.findall(".//spine/asset-clip/asset-clip")
    assert alts and all(a.get("enabled") is None for a in alts)


def slots(root):
    """[(spine element, [(lane, take name, enabled, duration)])] - lane 0 = the spine item itself."""
    names = {a.get("id"): a.get("name") for a in root.iter("asset")}
    out = []
    for el in root.find(".//spine"):
        clips = []
        if el.tag == "asset-clip":
            clips.append((0, names[el.get("ref")], el.get("enabled") != "0", secs(el.get("duration"))))
        for c in el.findall("asset-clip"):
            clips.append((int(c.get("lane")), names[c.get("ref")], c.get("enabled") != "0",
                          secs(c.get("duration"))))
        out.append((el, clips))
    return out


def test_tracks_layout_one_track_per_take(tmp_path):
    segs, root = fcp(tmp_path, enable_alts=False, layout="tracks")
    lane_of = {"mikey1": 0, "mikey2": 1, "claire1": 2}     # grouped by character, takes in order
    sl = slots(root)
    assert len(sl) == len(segs)
    pos = Fraction(0)
    for (el, clips), s in zip(sl, segs):
        assert secs(el.get("offset")) == pos
        pos += secs(el.get("duration"))
        for lane, name, _, _ in clips:
            assert lane == lane_of[name]
        enabled = [name for _, name, on, _ in clips if on]
        assert enabled == [f"{s.character.lower()}{s.primary.take.index + 1}"]   # only the chosen take
        if s.character == "CLAIRE":
            assert el.tag == "gap"                                             # track 1 unused here
    assert secs(root.find(".//sequence").get("duration")) == pos
    mikey = sl[0][1]
    assert [(lane, name, on) for lane, name, on, _ in mikey] == [(0, "mikey1", True), (1, "mikey2", False)]


def test_tracks_layout_alternate_on_track_one(tmp_path):
    # Main MIKEY take fluffs line 1, so pick-best puts take 2 on its track and take 1 becomes the
    # (disabled) alternate on track 1, stretched to the line's length.
    words = words_for([1, 4, 7, 9, 13, 16])
    words[0]["word"] = "dud"
    segs, root = fcp(tmp_path, enable_alts=False, layout="tracks", opts=Options(pick_best=True),
                     words={("MIKEY", 0): words})
    el, clips = slots(root)[0]
    assert segs[0].primary.take.index == 1
    assert el.tag == "asset-clip" and el.get("enabled") == "0"
    by_lane = {lane: (name, on, dur) for lane, name, on, dur in clips}
    assert by_lane[0][:2] == ("mikey1", False) and by_lane[1][:2] == ("mikey2", True)
    assert by_lane[0][2] == by_lane[1][2] == secs(el.get("duration"))


def test_tracks_layout_enable_alts(tmp_path):
    _, root = fcp(tmp_path, enable_alts=True, layout="tracks")
    assert all(on for _, clips in slots(root) for _, _, on, _ in clips)
