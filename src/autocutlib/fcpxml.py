"""Write the edit as FCPXML 1.8 for DaVinci Resolve (File > Import > Timeline)."""

from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import quoteattr

from .media import timecode_frames
from .report import Reporter


def ftime(t):
    """Seconds (Fraction) -> FCPXML rational time."""
    t = Fraction(t)
    return f"{t.numerator}s" if t.denominator == 1 else f"{t.numerator}/{t.denominator}s"


def _frames(clip):
    """Clip in/out snapped to its take's frames: floor the in, ceil the out."""
    fd = float(clip.take.info["fd"])
    f0 = int(clip.start / fd)
    f1 = -int(-clip.end // fd)
    return f0, max(f1 - f0, 1)


def write_fcpxml(segs, takes, out_path, title, enable_alts=False, reporter=None, layout="tracks"):
    """
    segs    Segments from edit.build_segments
    takes   every Take, in order (grouped by character); the very first one sets the timeline's format
    layout  "tracks":  one video+audio track per take, in `takes` order (V1/A1 = first character's
                       main take, ...). Each line's chosen clip is enabled on its take's track; the
                       other takes of that line sit disabled on their own tracks.
            "stacked": every chosen clip on V1/A1, the other takes stacked on the lanes above it.
    """
    rep = reporter or Reporter()
    seq = takes[0].info
    seq_fd = seq["fd"]
    for t in takes[1:]:
        if t.info["rate"] != seq["rate"]:
            rep.warn(f"{t.label} is {float(t.info['rate']):.3f} fps but the timeline is "
                     f"{float(seq['rate']):.3f} fps. Resolve will conform, but cuts may be a frame off.")

    res, ids = [], {}
    for n, t in enumerate(takes):
        m = t.info
        fmt_id, asset_id = f"r{2 * n + 1}", f"r{2 * n + 2}"
        tc_frames, drop = timecode_frames(m["timecode"], m["rate"])
        ids[t.key] = {"fmt": fmt_id, "asset": asset_id, "tc": tc_frames,
                      "tcfmt": "DF" if drop else "NDF"}
        fd = m["fd"]
        total_frames = int(m["duration"] * float(m["rate"]))
        res.append(f'    <format id="{fmt_id}" frameDuration="{ftime(fd)}" '
                   f'width="{m["width"]}" height="{m["height"]}"/>')
        audio = (f' hasAudio="1" audioSources="1" audioChannels="{m["channels"]}" '
                 f'audioRate="{m["sample_rate"]}"' if m["channels"] else "")
        res.append(f'    <asset id="{asset_id}" name={quoteattr(Path(t.path).stem)} '
                   f'src={quoteattr(Path(t.path).resolve().as_uri())} '
                   f'start="{ftime(tc_frames * fd)}" duration="{ftime(total_frames * fd)}" '
                   f'hasVideo="1" format="{fmt_id}"{audio}/>')

    def source_range(clip):
        """(start in the asset's timecode, duration rounded to timeline frames)"""
        f0, nf = _frames(clip)
        fd = clip.take.info["fd"]
        start = (ids[clip.take.key]["tc"] + f0) * fd
        dur = max(round(nf * fd / seq_fd), 1) * seq_fd
        return start, dur

    def end_of(take):
        m = take.info
        return (ids[take.key]["tc"] + int(m["duration"] * float(m["rate"]))) * m["fd"]

    def disabled(primary):
        return "" if primary or enable_alts else " enabled=" + quoteattr("0")

    if layout == "tracks":
        clips, offset = _tracks_spine(segs, takes, ids, source_range, end_of, disabled, seq_fd, rep)
    else:
        clips, offset = _stacked_spine(segs, ids, source_range, enable_alts, seq_fd)
    xml = _document(res, clips, ids[takes[0].key]["fmt"], offset, title)
    Path(out_path).write_text(xml, encoding="utf-8")
    return float(offset)


def _clip_name(n, s, clip, primary):
    return f"{n + 1:03d} {s.character} t{clip.take.index + 1}" + ("" if primary else " (alt)")


def _tracks_spine(segs, takes, ids, source_range, end_of, disabled, seq_fd, rep):
    """Spine = track 1 (the first take). Per line: the track-1 clip if that line has one, else a gap
    of the line's length; every other take's clip is connected to it on lane (track - 1)."""
    track = {t.key: i for i, t in enumerate(takes)}
    clips, offset, dropped = [], Fraction(0), 0
    for n, s in enumerate(segs):
        p_start, slot = source_range(s.primary)
        note = f"{s.character}: {s.text}"[:240]
        items = [(s.primary, True)] + [(a, False) for a in s.alts]
        base = next(((c, prim) for c, prim in items if track[c.take.key] == 0), None)
        parent_start = Fraction(0)
        if base is not None:
            c, prim = base
            c_start = p_start if prim else source_range(c)[0]
            if c_start + slot <= end_of(c.take):
                parent_start = c_start
            else:                    # an alternate too short to fill the slot on track 1
                base, dropped = None, dropped + 1

        children = []
        for c, prim in items:
            if base is not None and c is base[0]:
                continue
            if track[c.take.key] == 0:
                continue             # dropped alternate (see above)
            cid = ids[c.take.key]
            c_start, c_dur = (p_start, slot) if prim else source_range(c)
            marker = (f'>\n              <marker start="{ftime(c_start)}" duration="{ftime(seq_fd)}" '
                      f'value={quoteattr(note)}/>\n            </asset-clip>') if prim else "/>"
            # A connected clip's offset is in its parent's time: anchor it to the parent's start.
            children.append(
                f'            <asset-clip ref="{cid["asset"]}" lane="{track[c.take.key]}" '
                f'name={quoteattr(_clip_name(n, s, c, prim))} offset="{ftime(parent_start)}" '
                f'start="{ftime(c_start)}" duration="{ftime(c_dur)}" format="{cid["fmt"]}" '
                f'tcFormat="{cid["tcfmt"]}"{disabled(prim)}{marker}')

        if base is not None:
            c, prim = base
            cid = ids[c.take.key]
            if prim:
                children.append(f'            <marker start="{ftime(parent_start)}" '
                                f'duration="{ftime(seq_fd)}" value={quoteattr(note)}/>')
            clips.append(
                f'          <asset-clip ref="{cid["asset"]}" name={quoteattr(_clip_name(n, s, c, prim))} '
                f'offset="{ftime(offset)}" start="{ftime(parent_start)}" duration="{ftime(slot)}" '
                f'format="{cid["fmt"]}" tcFormat="{cid["tcfmt"]}"{disabled(prim)}>\n'
                + "\n".join(children) + '\n          </asset-clip>')
        else:
            clips.append(
                f'          <gap name={quoteattr(f"{n + 1:03d}")} offset="{ftime(offset)}" start="0s" '
                f'duration="{ftime(slot)}">\n' + "\n".join(children) + '\n          </gap>')
        offset += slot
    if dropped:
        rep.warn(f"{dropped} alternate clip(s) on track 1 were too short to fill their slot and were left out.")
    return clips, offset


def _stacked_spine(segs, ids, source_range, enable_alts, seq_fd):
    clips, offset = [], Fraction(0)
    for n, s in enumerate(segs):
        p = s.primary
        pid = ids[p.take.key]
        start, dur = source_range(p)
        take_note = f" (take {p.take.index + 1})" if p.take.index else ""
        name = f"{n + 1:03d} {s.character}{take_note}"
        note = f"{s.character}: {s.text}"[:240]
        children = []
        for lane, a in enumerate(s.alts, 1):
            aid = ids[a.take.key]
            a_start, a_dur = source_range(a)
            # A connected clip's offset is in its parent's time: anchor it to the parent's start.
            children.append(
                f'            <asset-clip ref="{aid["asset"]}" lane="{lane}" '
                f'name={quoteattr(f"{n + 1:03d} {s.character} alt take {a.take.index + 1}")} '
                f'offset="{ftime(start)}" start="{ftime(a_start)}" duration="{ftime(a_dur)}" '
                f'format="{aid["fmt"]}" tcFormat="{aid["tcfmt"]}"'
                f'{"" if enable_alts else " enabled=" + quoteattr("0")}/>')
        children.append(f'            <marker start="{ftime(start)}" duration="{ftime(seq_fd)}" '
                        f'value={quoteattr(note)}/>')
        clips.append(
            f'          <asset-clip ref="{pid["asset"]}" name={quoteattr(name)} '
            f'offset="{ftime(offset)}" start="{ftime(start)}" duration="{ftime(dur)}" '
            f'format="{pid["fmt"]}" tcFormat="{pid["tcfmt"]}">\n'
            + "\n".join(children) +
            '\n          </asset-clip>')
        offset += dur
    return clips, offset


def _document(res, clips, seq_fmt, offset, title):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.8">
  <resources>
{chr(10).join(res)}
  </resources>
  <library>
    <event name={quoteattr(title)}>
      <project name={quoteattr(title)}>
        <sequence format="{seq_fmt}" duration="{ftime(offset)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
{chr(10).join(clips)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''
