"""
autocut - Assemble a dialogue scene from per-character takes, using the screenplay.

You shot the scene once (or more) framed on each character, with the other actor
reading their lines off camera. This:

  1. Reads the dialogue (who says what, in order) from the screenplay PDF.
  2. Transcribes each take with WhisperX (word-level timestamps, cached).
  3. Aligns the full screenplay dialogue against each take's transcript.
  4. For every line, takes the clip from the video where that character is on screen.
  5. Writes an FCPXML timeline for DaVinci Resolve (File > Import > Timeline),
     and optionally renders a quick MP4 preview with ffmpeg.

Give --video more than once for the same character to add alternate takes. The first
one is the main take (V1); the others are stacked above it as disabled clips.
Files Resolve can't read (e.g. .mpeg with MP2 audio) are converted to ProRes .mov.
--voice NAME=VOICE changes a character's voice with ElevenLabs via fal.ai (set FAL_KEY).

Example:
  python autocut.py --script scene.pdf --pages 3-4 \\
      --video MIKEY=mikey_take.mov --video CLAIRE=claire_take.mov \\
      --video MIKEY=mikey_take2.mov \\
      --out scene_cut.fcpxml --preview scene_preview.mp4

Check the screenplay parse first (no transcription):
  python autocut.py --script scene.pdf --pages 3-4 --dump
"""

import argparse
import sys

from .pipeline import Options, run
from .report import AutocutError
from .screenplay import dump_text, load_dialogue


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="autocut", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--script", required=True, help="screenplay PDF (must have selectable text)")
    p.add_argument("--pages", help="only these pages, e.g. 3-4 or 3,5-6")
    p.add_argument("--video", action="append", default=[], metavar="NAME=PATH",
                   help="character name as written in the screenplay = a take framed on them; "
                        "repeat a name for alternate takes (first = main)")
    p.add_argument("--out", default="autocut.fcpxml", help="FCPXML to import into Resolve")
    p.add_argument("--preview", help="also render a quick MP4 of the cut")
    p.add_argument("--dump", action="store_true", help="print the parsed dialogue and exit")
    p.add_argument("--pre", type=float, default=0.20, help="seconds of lead-in before each line")
    p.add_argument("--post", type=float, default=0.30, help="seconds of tail after each line")
    p.add_argument("--merge-gap", type=float, default=3.0,
                   help="merge a character's consecutive lines if the gap is under this many seconds")
    p.add_argument("--no-merge", action="store_true", help="always cut per screenplay line")
    p.add_argument("--pick-best", action="store_true",
                   help="put whichever take matched the screenplay best on V1, per clip "
                        "(default: always the main take)")
    p.add_argument("--enable-alts", action="store_true",
                   help="leave the stacked alternate takes enabled (default: disabled)")
    p.add_argument("--model", default="large-v3", help="Whisper model (large-v3, medium, small...)")
    p.add_argument("--language", default="en")
    p.add_argument("--retranscribe", action="store_true", help="ignore cached transcripts")
    p.add_argument("--no-convert", action="store_true",
                   help="don't convert files Resolve can't read to ProRes")
    p.add_argument("--voice", dest="voices", action="append", default=[], metavar="NAME=VOICE",
                   help="change NAME's voice with ElevenLabs via fal.ai (needs FAL_KEY), "
                        "e.g. ARNOLD=Brian or a voice ID; ~$0.30 per minute of used dialogue")
    p.add_argument("--voice-seed", type=int, default=42,
                   help="seed for the voice changer; the same seed gives the same rendition, "
                        "try others if you don't like it (default 42)")
    p.add_argument("--voice-stability", type=float, metavar="PERCENT",
                   help="voice changer stability, 0-100 (higher = steadier, flatter delivery); "
                        "default: the voice's own setting, usually 50")
    p.add_argument("--voice-denoise", action="store_true",
                   help="have the voice changer remove background noise first")
    p.add_argument("--convert-dir", help="put ProRes conversions here (default: next to each original)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        lines = load_dialogue(args.script, args.pages)
        if args.dump or not args.video:
            print(dump_text(lines))
            if not args.video:
                print("\nAdd --video NAME=PATH for each character to build the cut.")
            return

        videos = []
        for v in args.video:
            if "=" not in v:
                raise AutocutError(f"--video must look like NAME=path, got: {v}")
            videos.append(tuple(v.split("=", 1)))

        if args.voice_stability is not None and not 0 <= args.voice_stability <= 100:
            raise AutocutError("--voice-stability must be between 0 and 100")
        voices = {}
        for v in args.voices:
            if "=" not in v:
                raise AutocutError(f"--voice must look like NAME=VOICE, got: {v}")
            name, voice = v.split("=", 1)
            voices[name.strip().upper()] = voice.strip()

        opts = Options(pages=args.pages, pre=args.pre, post=args.post, merge_gap=args.merge_gap,
                       no_merge=args.no_merge, pick_best=args.pick_best,
                       enable_alts=args.enable_alts, model=args.model, language=args.language,
                       retranscribe=args.retranscribe, convert=not args.no_convert,
                       convert_dir=args.convert_dir, voices=voices,
                       voice_denoise=args.voice_denoise, voice_seed=args.voice_seed,
                       voice_stability=None if args.voice_stability is None else args.voice_stability / 100)
        run(args.script, videos, args.out, args.preview, opts)
    except AutocutError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
