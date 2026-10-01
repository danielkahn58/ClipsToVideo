"""Change a character's voice with ElevenLabs' voice changer, via fal.ai.

Only the stretches of each take that the cut actually uses (primary and alternate clips, plus
a little padding) are sent, since it's billed per minute. All of a character's stretches, from
all their takes, go in ONE request (joined with short silences), with a fixed seed: converting
each line separately makes the voice drift in pitch and timbre from clip to clip. The result is
split back into stretches and dropped in at the same positions over the take's original audio,
and the result is muxed with the untouched video into `<take>.<voice>.mov`, which then
stands in for the take in the FCPXML and preview. The voice changer keeps the performance's
timing, so the cut computed from the original audio still lines up.

Converted stretches are cached (~/Library/Caches/autocut/voice on macOS), so re-running a cut
doesn't pay for the same audio twice.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from .media import _writable_dir
from .report import AutocutError, Reporter

FAL_APP = "fal-ai/elevenlabs/voice-changer"
PRICE_PER_MIN = 0.30          # USD, fal's listed price for this model
PAD = 0.4                     # seconds of extra audio either side of each used clip
JOIN_GAP = 1.0                # stretches closer than this are merged into one
SILENCE = 0.6                 # gap between stretches inside a batched request
MAX_BATCH = 240.0             # seconds of audio per request; longer is split into batches
DEFAULT_SEED = 42

# ElevenLabs' standard voices. Any other voice name or ID that fal accepts can be typed in.
VOICES = ["Rachel", "Aria", "Sarah", "Laura", "Charlotte", "Alice", "Matilda", "Jessica", "Lily",
          "River", "Roger", "Charlie", "George", "Callum", "Liam", "Will", "Eric", "Chris",
          "Brian", "Daniel", "Bill"]


def cache_dir():
    base = (Path.home() / "Library" / "Caches" if sys.platform == "darwin"
            else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")))
    d = base / "autocut" / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def require_fal():
    try:
        import fal_client  # noqa: F401
    except ImportError:
        raise AutocutError("fal-client isn't installed. Run: pip install -e .") from None
    if not (os.environ.get("FAL_KEY") or (os.environ.get("FAL_KEY_ID") and os.environ.get("FAL_KEY_SECRET"))):
        raise AutocutError("No fal.ai API key. Set it in the web UI (Options > Voice), or "
                           "export FAL_KEY=... before running.")


def call_fal(audio_path, voice, denoise, rep, seed=DEFAULT_SEED):
    """Send one audio file through the voice changer; returns the URL of the result."""
    import fal_client
    url = fal_client.upload_file(audio_path)

    def on_update(status):
        for entry in getattr(status, "logs", None) or []:
            msg = entry.get("message") if isinstance(entry, dict) else None
            if msg:
                rep.log(f"    fal: {msg}")

    args = {"audio_url": url, "voice": voice, "remove_background_noise": bool(denoise)}
    if seed is not None:
        args["seed"] = int(seed)
    try:
        result = fal_client.subscribe(FAL_APP, args, with_logs=True, on_queue_update=on_update)
    except Exception as e:  # noqa: BLE001 - surface fal's message (bad voice name, no credit...)
        raise AutocutError(f"fal.ai voice changer failed: {e}") from None
    audio = result.get("audio") if isinstance(result, dict) else None
    out = audio.get("url") if isinstance(audio, dict) else (result or {}).get("audio_url")
    if not out:
        raise AutocutError(f"Unexpected reply from fal.ai: {json.dumps(result)[:300]}")
    return out


def merge_ranges(ranges, duration):
    """Pad, clamp, and join (start, end) ranges that overlap or nearly touch."""
    out = []
    for s, e in sorted((max(0.0, s - PAD), min(duration, e + PAD)) for s, e in ranges):
        if out and s - out[-1][1] < JOIN_GAP:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in out]


def _ffmpeg(args, what):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AutocutError(f"ffmpeg failed {what}:\n{r.stderr.strip()}")


def _duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise AutocutError(f"Couldn't read the converted audio ({path.name}).") from None


def _file_key(path):
    st = Path(path).stat()
    return f"{Path(path).resolve()}|{st.st_size}|{int(st.st_mtime)}"


def batches(items, limit=MAX_BATCH):
    """Split [(path, start, end)] into consecutive groups of at most `limit` seconds."""
    out, cur, total = [], [], 0.0
    for it in items:
        d = it[2] - it[1]
        if cur and total + d > limit:
            out.append(cur)
            cur, total = [], 0.0
        cur.append(it)
        total += d + SILENCE
    if cur:
        out.append(cur)
    return out


def convert_batch(items, voice, denoise, seed, rep, caller=None):
    """Convert several stretches [(take_path, start, end)] in ONE request, so they all get the
    same rendition of the voice. Returns ([48 kHz mono WAV per stretch, each exactly its input
    length], seconds newly sent to the API)."""
    sig = json.dumps([[_file_key(p), s, e] for p, s, e in items] + [voice, bool(denoise), seed])
    key = hashlib.sha1(sig.encode()).hexdigest()[:20]
    outs = [cache_dir() / f"{key}-{i}.wav" for i in range(len(items))]
    if all(o.exists() for o in outs):
        return outs, 0.0

    tmp = cache_dir() / f"{key}.work"
    tmp.mkdir(exist_ok=True)
    try:
        # One input file: stretch, silence, stretch, silence, ...
        cmd, labels, offsets, at = [], [], [], 0.0
        for i, (p, s, e) in enumerate(items):
            cmd += ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(p)]
            labels.append(f"[{i}:a:0]aresample=44100,aformat=channel_layouts=mono,"
                          f"apad,atrim=duration={e - s:.3f}[p{i}]")
            offsets.append(at)
            at += (e - s) + SILENCE
        gaps = [f"anullsrc=r=44100:cl=mono,atrim=duration={SILENCE}[g{i}]" for i in range(len(items))]
        chain = "".join(f"[p{i}][g{i}]" for i in range(len(items)))
        graph = ";".join(labels + gaps + [f"{chain}concat=n={2 * len(items)}:v=0:a=1[out]"])
        src = tmp / "in.wav"
        _ffmpeg([*cmd, "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", str(src)],
                "preparing audio for the voice changer")

        raw = tmp / "out.raw"
        urllib.request.urlretrieve((caller or call_fal)(src, voice, denoise, rep, seed), raw)
        full = tmp / "out.wav"
        _ffmpeg(["-i", str(raw), "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(full)],
                "decoding the converted audio")

        # The voice changer keeps timing; if the length drifted at all, scale positions to match.
        scale = _duration(full) / at
        if abs(scale - 1) > 0.02:
            rep.warn(f"Converted audio is {abs(scale - 1):.0%} {'longer' if scale > 1 else 'shorter'} "
                     "than what was sent; re-timing it to fit.")
        for (_, s, e), off, out in zip(items, offsets, outs):
            d = e - s
            fit = f"atempo={scale:.5f}," if abs(scale - 1) > 0.002 else ""
            _ffmpeg(["-ss", f"{off * scale:.4f}", "-t", f"{d * scale:.4f}", "-i", str(full),
                     "-af", f"{fit}apad,atrim=duration={d:.3f}", "-c:a", "pcm_s16le", str(out)],
                    "splitting the converted audio")
    finally:
        for f in tmp.iterdir():
            f.unlink(missing_ok=True)
        tmp.rmdir()
    return outs, at


def voiced_path(take_path, voice, out_dir=None):
    safe = re.sub(r"[^\w\-]+", "_", voice).strip("_") or "voice"
    folder = Path(out_dir) if out_dir else Path(take_path).parent
    return folder / f"{Path(take_path).stem}.{safe}.mov"


def build_voiced_takes(takes_ranges, voice, opts, rep, caller=None):
    """Voice-change one character. takes_ranges: [(Take, [(start, end), ...])] for all their
    takes. Returns {take.key: path of the voiced .mov}."""
    seed = opts.voice_seed
    plan = []                       # (take, merged ranges, dst)
    for take, ranges in takes_ranges:
        ranges = merge_ranges(ranges, take.info["duration"])
        if not ranges:
            continue
        dst = voiced_path(take.path, voice, opts.convert_dir)
        if not _writable_dir(dst.parent):
            dst = voiced_path(take.path, voice, opts.fallback_dir or cache_dir())
            _writable_dir(dst.parent)
        plan.append((take, ranges, dst))

    # Everything for this character in as few requests as possible (normally one).
    items = [(t.path, s, e) for t, ranges, _ in plan for s, e in ranges]
    groups = batches(items)
    wavs, sent = [], 0.0
    for n, group in enumerate(groups, 1):
        if len(groups) > 1:
            rep.log(f"  request {n}/{len(groups)} ({sum(e - s for _, s, e in group):.0f}s of audio)")
        rep.progress((n - 1) / len(groups), f"converting {n}/{len(groups)}")
        got, new = convert_batch(group, voice, opts.voice_denoise, seed, rep, caller)
        wavs += got
        sent += new
    if sent:
        rep.log(f"  sent {sent:.0f}s of audio to the voice changer (about ${sent / 60 * PRICE_PER_MIN:.2f})")
    else:
        rep.log("  all converted audio was cached")

    result, k = {}, 0
    for take, ranges, dst in plan:
        pieces = [(s, e, wavs[k + i]) for i, (s, e) in enumerate(ranges)]
        k += len(ranges)
        sig = {"source": _file_key(take.path), "pieces": [str(w) for _, _, w in pieces],
               "ranges": ranges}
        stamp = Path(str(dst) + ".json")
        if not (dst.exists() and stamp.exists() and _read_json(stamp) == sig):
            _mux(take, pieces, dst)
            stamp.write_text(json.dumps(sig))
        result[take.key] = dst
    rep.progress(1.0, "done")
    return result


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _mux(take, pieces, dst):
    """Take's video (copied) + its audio with the converted stretches laid over it."""
    info = take.info
    inputs = ["-i", str(take.path)]
    for _, _, wav in pieces:
        inputs += ["-i", str(wav)]
    mute = "+".join(f"between(t,{s},{e})" for s, e, _ in pieces)
    if info["channels"]:
        base = (f"[0:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                f"volume=volume='if({mute},0,1)':eval=frame[base]")
    else:
        base = f"anullsrc=r=48000:cl=stereo,atrim=duration={info['duration']:.3f}[base]"
    parts = [base]
    for i, (s, _, _) in enumerate(pieces, 1):
        ms = int(round(s * 1000))
        parts.append(f"[{i}:a]afade=t=in:d=0.02,areverse,afade=t=in:d=0.02,areverse,"
                     f"aformat=channel_layouts=stereo,adelay={ms}|{ms}[v{i}]")
    mix = "[base]" + "".join(f"[v{i}]" for i in range(1, len(pieces) + 1))
    parts.append(f"{mix}amix=inputs={len(pieces) + 1}:normalize=0:duration=first[a]")

    tmp = dst.with_name(dst.stem + ".partial.mov")
    tc = ["-timecode", info["timecode"]] if info.get("timecode") else []
    try:
        _ffmpeg([*inputs, "-filter_complex", ";".join(parts), "-map", "0:v:0", "-map", "[a]",
                 "-c:v", "copy", "-c:a", "pcm_s16le", *tc, "-map_metadata", "0", str(tmp)],
                f"writing {dst.name}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dst)


def preview(take_path, voice, start, seconds, denoise, seed=DEFAULT_SEED, rep=None, caller=None):
    """Convert a short stretch of a take, for auditioning a voice. Returns the WAV path."""
    from .media import probe
    rep = rep or Reporter()
    dur = probe(take_path)["duration"]
    start = max(0.0, min(start, max(dur - seconds, 0.0)))
    (wav,), _ = convert_batch([(Path(take_path), round(start, 3), round(min(start + seconds, dur), 3))],
                              voice, denoise, seed, rep, caller)
    return wav
