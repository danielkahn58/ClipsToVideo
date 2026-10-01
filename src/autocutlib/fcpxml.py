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


def write_fcpxml(segs, takes, out_path, title, enable_alts=False, reporter=None):
    """
    segs    Segments from edit.build_segments
    takes   every Take, in order; the very first one sets the timeline's format
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

    seq_fmt = ids[takes[0].key]["fmt"]
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
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
    Path(out_path).write_text(xml, encoding="utf-8")
    return float(offset)
