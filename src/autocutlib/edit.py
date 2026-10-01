"""Turn per-take alignments into an edit: one segment per line (or merged run of lines),
each with a primary clip for V1 and alternate clips from the character's other takes."""

from dataclasses import dataclass, field
from pathlib import Path

from .report import Reporter


@dataclass
class Take:
    character: str
    index: int            # 0 = the main take for this character
    path: Path            # the file the edit uses (the ProRes copy, if one was made)
    source: Path          # the file as given
    info: dict
    voice: str = None     # set when the audio was voice-changed

    @property
    def key(self):
        return (self.character, self.index)

    @property
    def label(self):
        return f"{self.character} take {self.index + 1}"


@dataclass
class Clip:
    take: Take
    start: float
    end: float
    ratio: float


@dataclass
class Segment:
    character: str
    lines: list            # 1-based screenplay line numbers
    text: str
    primary: Clip
    alts: list = field(default_factory=list)     # Clips from other takes, in take order

    @property
    def start(self):
        return self.primary.start

    @property
    def end(self):
        return self.primary.end

    @property
    def ratio(self):
        return self.primary.ratio

    @property
    def video(self):
        return self.primary.take.path


def line_clip(take, toks, span, opts):
    """Padded in/out for one line in one take, or None if the line wasn't found there."""
    if span is None:
        return None
    t0, t1 = toks[span["first"]]["start"], toks[span["last"]]["end"]
    prev_end = toks[span["first"] - 1]["end"] if span["first"] > 0 else 0.0
    next_start = toks[span["last"] + 1]["start"] if span["last"] + 1 < len(toks) else None

    # Pad, but stop halfway to the neighbouring (off-screen) line so it doesn't bleed in.
    start = max(t0 - opts.pre, (prev_end + t0) / 2, 0.0)
    end = t1 + opts.post
    if next_start is not None:
        end = min(end, (t1 + next_start) / 2)
    return Clip(take, start, end, span["ratio"])


def _continues(prev, nxt, opts):
    """Same speaker continues within a take: keep the natural pause instead of cutting."""
    return nxt.start >= prev.end - 0.5 and nxt.start - prev.end < opts.merge_gap


def build_segments(lines, takes, transcripts, spans, opts, reporter=None):
    """
    lines        screenplay lines (already filtered to characters that have takes)
    takes        {character: [Take, ...]} in order, first = main take
    transcripts  {take.key: transcript tokens}
    spans        {take.key: align() result for all lines}

    Lines are grouped (merged) by their timing in the character's main take - or, for a line
    the main take is missing, the first take that has it. Each group becomes one Segment.
    The primary clip is the main take, or with opts.pick_best the take whose words matched
    the screenplay best; every other take that contains the whole group is an alternate.
    """
    rep = reporter or Reporter()
    groups = []
    for i, ln in enumerate(lines):
        per_take = {t.key: line_clip(t, transcripts[t.key], spans[t.key][i], opts)
                    for t in takes[ln.character]}
        ref = next((t for t in takes[ln.character] if per_take[t.key]), None)
        if ref is None:
            rep.warn(f"Line {i + 1} ({ln.character}) not found in any of their takes - skipped: "
                     f"{ln.text[:60]}")
            continue
        last = groups[-1] if groups else None
        if (last and not opts.no_merge and last["character"] == ln.character
                and per_take.get(last["ref"])
                and _continues(last["clips"][last["ref"]][-1], per_take[last["ref"]], opts)):
            last["lines"].append(i)
            for k, c in per_take.items():
                last["clips"][k].append(c)
        else:
            groups.append({"character": ln.character, "ref": ref.key, "lines": [i],
                           "clips": {k: [c] for k, c in per_take.items()}})

    return [_segment(g, lines, takes[g["character"]], opts) for g in groups]


def _segment(g, lines, char_takes, opts):
    candidates = []    # (score, take index, Clip) for takes that contain the whole group
    for t in char_takes:
        cl = g["clips"][t.key]
        if any(c is None for c in cl):
            continue
        if t.key != g["ref"] and not all(_continues(a, b, opts) for a, b in zip(cl, cl[1:])):
            continue        # lines are scattered in this take; one clip can't cover them
        ratios = [c.ratio for c in cl]
        clip = Clip(t, cl[0].start, max(c.end for c in cl), min(ratios))
        candidates.append(((min(ratios), sum(ratios) / len(ratios)), t.index, clip))

    if opts.pick_best:
        primary = max(candidates, key=lambda c: (c[0], -c[1]))[2]
    else:
        primary = next(c[2] for c in candidates if c[2].take.key == g["ref"])
    alts = [c[2] for c in sorted(candidates, key=lambda c: c[1]) if c[2] is not primary]
    return Segment(g["character"], [i + 1 for i in g["lines"]],
                   " / ".join(lines[i].text for i in g["lines"]), primary, alts)
