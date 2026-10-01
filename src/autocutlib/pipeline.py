"""The whole job as one function, shared by the CLI and the web UI's worker."""

from dataclasses import asdict, dataclass
from pathlib import Path

from .align import align
from .edit import Take, build_segments
from .fcpxml import write_fcpxml
from .media import convert_for_resolve, probe, require_ffmpeg, resolve_problems
from .preview import render_preview
from .report import AutocutError, Reporter
from .screenplay import load_dialogue
from .text import fmt_tc
from .transcribe import Transcriber, transcript_tokens
from .voice import DEFAULT_SEED, build_voiced_takes, require_fal

LOW_MATCH = 0.6     # lines that matched fewer of their words than this get flagged


@dataclass
class Options:
    pages: str = None
    pre: float = 0.20
    post: float = 0.30
    merge_gap: float = 3.0
    no_merge: bool = False
    pick_best: bool = False
    enable_alts: bool = False
    model: str = "large-v3"
    language: str = "en"
    retranscribe: bool = False
    convert: bool = True          # convert files Resolve can't read to ProRes
    convert_dir: str = None       # None = next to each original
    fallback_dir: str = None      # used if an original's folder isn't writable
    voices: dict = None           # {CHARACTER: ElevenLabs voice name or ID}, via fal.ai
    voice_denoise: bool = False   # ask the voice changer to strip background noise first
    voice_seed: int = DEFAULT_SEED  # same seed -> same rendition of the voice; change to vary it
    voice_stability: float = None   # 0..1; None = the voice's own default (ElevenLabs: usually 0.5)

    @classmethod
    def from_dict(cls, d):
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def normalize_videos(videos):
    """{name: path or [paths]} -> {NAME: [Path, ...]}; also accepts [(name, path), ...]."""
    out = {}
    items = videos.items() if isinstance(videos, dict) else videos
    for name, paths in items:
        if isinstance(paths, (str, Path)):
            paths = [paths]
        for p in paths:
            out.setdefault(name.strip().upper(), []).append(Path(p).expanduser())
    return out


def prepare_takes(videos, opts, rep):
    """Probe every take, converting the ones Resolve can't read. Returns {char: [Take]}."""
    takes = {}
    for char, paths in videos.items():
        for idx, src in enumerate(paths):
            if not src.exists():
                raise AutocutError(f"File not found: {src}")
            info = probe(src)
            path, problems = src, resolve_problems(info)
            if problems and opts.convert:
                rep.log(f"{src.name}: Resolve can't use {', '.join(problems)}; converting.")
                path = convert_for_resolve(src, info, opts.convert_dir, opts.fallback_dir, rep)
                info = probe(path)
            elif problems:
                rep.warn(f"{src.name}: Resolve may not read {', '.join(problems)} (conversion is off).")
            takes.setdefault(char, []).append(Take(char, idx, path, src, info))
    return takes


def run(script, videos, out, preview=None, opts=None, reporter=None):
    """Parse, transcribe, align, and write the FCPXML (and preview). Returns a result dict."""
    opts = opts or Options()
    rep = reporter or Reporter()
    require_ffmpeg()
    videos = normalize_videos(videos)
    if not videos:
        raise AutocutError("No takes given.")

    rep.stage("Reading screenplay")
    lines = load_dialogue(script, opts.pages)
    rep.log(f"Parsed {len(lines)} dialogue lines.")
    speakers = {ln.character for ln in lines}
    unknown = speakers - set(videos)
    if unknown:
        rep.warn(f"No take given for {', '.join(sorted(unknown))}; their lines will be dropped.")
    missing = set(videos) - speakers
    if missing:
        raise AutocutError(f"{', '.join(sorted(missing))} doesn't appear as a speaker in the parsed "
                           f"dialogue. Speakers found: {', '.join(sorted(speakers))}. "
                           "Check the dialogue listing (--dump).")
    lines = [ln for ln in lines if ln.character in videos]

    rep.stage("Checking media")
    takes = prepare_takes(videos, opts, rep)
    all_takes = [t for ts in takes.values() for t in ts]

    transcriber = Transcriber(opts.model, opts.language, opts.retranscribe, rep)
    transcripts, spans = {}, {}
    for n, t in enumerate(all_takes, 1):
        rep.stage(f"Transcribing {t.label} ({n}/{len(all_takes)}): {t.source.name}")
        aliases = [t.source] if t.source != t.path else []
        transcripts[t.key] = transcript_tokens(transcriber.words(t.path, aliases))
        # Align the WHOLE scene (all characters) to each take: the off-screen
        # actor's lines anchor the timing even though we only use this character's.
        spans[t.key] = align(lines, transcripts[t.key])

    rep.stage("Building the cut")
    segs = build_segments(lines, takes, transcripts, spans, opts, rep)
    if not segs:
        raise AutocutError("Nothing could be aligned. Check the dialogue listing and that the "
                           "right take is mapped to each name.")
    for line in cut_table_text(segs):
        rep.log(line)

    apply_voices(segs, takes, opts, rep)

    out = Path(out)
    total = write_fcpxml(segs, all_takes, out, out.stem, opts.enable_alts, rep)
    rep.log(f"\nWrote {out}: {len(segs)} clips, {fmt_tc(total)} long.")
    rep.log("In Resolve: File > Import > Timeline, then choose this file.")

    if preview:
        rep.stage("Rendering preview")
        render_preview(segs, all_takes[0].info, preview, rep)

    return result_dict(segs, all_takes, lines, out, preview, total, opts)


def apply_voices(segs, takes, opts, rep):
    """Swap in voice-changed copies of the takes of characters that have a voice set."""
    voices = {k.strip().upper(): v.strip() for k, v in (opts.voices or {}).items() if v and v.strip()}
    if not voices:
        return
    for char in sorted(set(voices) - set(takes)):
        rep.warn(f"Voice set for {char}, but {char} has no takes; ignored.")
    if not set(voices) & set(takes):
        return
    require_fal()
    for char, ts in takes.items():
        voice = voices.get(char)
        if not voice:
            continue
        takes_ranges = [(t, [(c.start, c.end) for s in segs for c in (s.primary, *s.alts) if c.take is t])
                        for t in ts]
        takes_ranges = [(t, r) for t, r in takes_ranges if r]
        if not takes_ranges:
            continue
        rep.stage(f"Changing voice: {char} -> {voice} ({len(takes_ranges)} take(s), seed {opts.voice_seed}, "
                  f"stability {'default' if opts.voice_stability is None else f'{opts.voice_stability:.0%}'})")
        paths = build_voiced_takes(takes_ranges, voice, opts, rep)
        for t, _ in takes_ranges:
            t.path, t.info, t.voice = paths[t.key], probe(paths[t.key]), voice


def cut_table_text(segs):
    rows = [f"\n{'#':>3}  {'CHARACTER':<12} {'TAKE':>4} {'IN':>8} {'OUT':>8}  MATCH  LINE"]
    for n, s in enumerate(segs, 1):
        flag = "  <- check" if s.ratio < LOW_MATCH else ""
        alts = "".join(f" [alt t{a.take.index + 1} {a.ratio:.0%}]" for a in s.alts)
        rows.append(f"{n:3d}  {s.character:<12} {s.primary.take.index + 1:>4} {fmt_tc(s.start):>8} "
                    f"{fmt_tc(s.end):>8}  {s.ratio:5.0%}  {s.text[:60]}{flag}{alts}")
    return rows


def result_dict(segs, takes, lines, out, preview, total, opts):
    def clip(c):
        return {"take": c.take.index + 1, "file": str(c.take.path), "start": round(c.start, 3),
                "end": round(c.end, 3), "ratio": round(c.ratio, 3)}
    used = {n for s in segs for n in s.lines}
    return {
        "fcpxml": str(Path(out).resolve()),
        "preview": str(Path(preview).resolve()) if preview else None,
        "duration": total,
        "low_match": LOW_MATCH,
        "options": asdict(opts),
        "takes": [{"character": t.character, "take": t.index + 1, "source": str(t.source),
                   "path": str(t.path), "converted": t.path != t.source, "voice": t.voice,
                   "width": t.info["width"], "height": t.info["height"],
                   "fps": float(t.info["rate"]), "duration": t.info["duration"]} for t in takes],
        "segments": [{"n": n, "character": s.character, "lines": s.lines, "text": s.text,
                      "primary": clip(s.primary), "alts": [clip(a) for a in s.alts],
                      "flag": s.ratio < LOW_MATCH} for n, s in enumerate(segs, 1)],
        "skipped": [{"n": i, "character": ln.character, "text": ln.text}
                    for i, ln in enumerate(lines, 1) if i not in used],
    }
