# autocut

Assemble a dialogue scene from per-character takes, using the screenplay.

You shoot the scene once (or more) framed on each character, with the other actor reading
lines off camera. autocut reads the dialogue from the screenplay PDF, transcribes each take
with WhisperX, aligns the screenplay against each transcript, and writes an **FCPXML timeline
for DaVinci Resolve**: for every line, the clip from the take where that character is on
screen. It can also render a quick MP4 preview, and change a character's voice with ElevenLabs'
voice changer (via fal.ai).

There is a **local web UI** (`autocut-web`) and the original **command line** (`python autocut.py` / `autocut`).

## Setup (macOS)

You need Homebrew's Python 3.11 and ffmpeg. macOS's built-in Python 3.9 is too old.

```bash
# 1. Tools
brew install python@3.11 ffmpeg

# 2. Get the code
git clone <this repo> autocut && cd autocut

# 3. A virtualenv on Python 3.11 (delete any old .venv made with the system Python first)
rm -rf .venv
/opt/homebrew/bin/python3.11 -m venv .venv      # Intel Macs: /usr/local/bin/python3.11
source .venv/bin/activate
python --version                                # should say 3.11.x

# 4. Install autocut with WhisperX (pulls in PyTorch; a few minutes)
pip install --upgrade pip
pip install -e ".[whisper]"
```

Run `source .venv/bin/activate` again in each new terminal before using autocut.

The first transcription downloads the Whisper model (`large-v3` is about 3 GB). Transcription
runs on the CPU (WhisperX can't use Apple's GPU) and takes a few minutes per take.
Results are cached next to each video as `<file>.words.json`, so later runs skip it.

## Web UI

```bash
autocut-web            # opens http://127.0.0.1:8765
```

1. **Screenplay**: choose the PDF, optionally a page range (`3-4`, `3,5-6`), and click
   *Parse dialogue*. Check the parsed lines and character names before transcribing anything.
2. **Takes**: for each character, add one or more takes. The first one is the **main** take
   (V1). Use the arrows to reorder.
   - **Choose…** opens the macOS file dialog. **Browse…** is a built-in folder browser.
     Either way, the file is **used where it is**, not copied. You can also paste a path.
     (Dragging a file from Finder into Terminal is a quick way to copy its path.)
   - **Upload…** copies the file into the workspace (`~/Movies/Autocut/uploads`). Use it
     only for files that aren't already on this Mac. The copy then becomes the file the
     timeline points to.
3. **Options**: these are the same as the CLI flags (see below). For voices, see
   [Changing a character's voice](#changing-a-characters-voice).
4. **Build the cut** shows each stage, progress, and the full log. Runs happen in a
   background process, so you can cancel them. Reloading the page reconnects to a running job.
5. **Result**: download the FCPXML (or *Show in Finder*), play or download the preview, and
   check the cut table. Lines that matched fewer than 60% of their words are flagged
   (**⚠**). Click a row to jump the preview to that clip.

Past runs are listed under *Past runs* (top right). Each run's FCPXML, preview, and log live in
`~/Movies/Autocut/jobs/<date>-<name>/`. Use `--home` or `AUTOCUT_HOME` to put the workspace
somewhere else, and `--port` to change the port.

The server only listens on 127.0.0.1 and only answers its own page.

## Command line

```bash
# Check the screenplay parse first (no transcription)
python autocut.py --script scene.pdf --pages 3-4 --dump

# Build the cut. Repeat --video for a character to add alternate takes (first = main).
python autocut.py --script scene.pdf --pages 3-4 \
    --video MIKEY=mikey_take.mov --video CLAIRE=claire_take.mov \
    --video MIKEY=mikey_take2.mpeg \
    --out scene_cut.fcpxml --preview scene_preview.mp4
```

`autocut ...` and `python -m autocutlib ...` are the same as `python autocut.py ...`.

| Option | Default | |
|---|---|---|
| `--pages` | all | e.g. `3-4` or `3,5-6` |
| `--pre` / `--post` | 0.20 / 0.30 | seconds of padding before / after each line (never more than halfway to the neighbouring line) |
| `--merge-gap` | 3.0 | merge a character's consecutive lines into one clip if the gap is under this |
| `--no-merge` | off | always cut per screenplay line |
| `--pick-best` | off | per clip, put whichever take matched the screenplay best on V1 (otherwise always the main take) |
| `--enable-alts` | off | leave the stacked alternates enabled (by default they're disabled) |
| `--model` | large-v3 | Whisper model: `large-v3`, `medium`, `small`, ... |
| `--language` | en | |
| `--retranscribe` | off | ignore cached `.words.json` transcripts |
| `--voice NAME=VOICE` | — | change NAME's voice with ElevenLabs via fal.ai, e.g. `ARNOLD=Brian` (repeat per character) |
| `--voice-denoise` | off | have the voice changer strip background noise first |
| `--no-convert` | off | don't convert files Resolve can't read |
| `--convert-dir` | next to original | where ProRes conversions go |

## How the timeline is built

- **V1** has one clip per line. Consecutive lines by the same character are one clip,
  unless `--no-merge` is set or the gap is longer than `--merge-gap`. Merging is decided
  from timing in the main take. Each clip has a marker with the line text.
- **Alternate takes** are stacked above as connected clips (lane 1 = take 2, lane 2 =
  take 3, ...), starting at the same point and **disabled**. In Resolve, enable the one you
  want and disable V1. An alternate is only added when that take contains the whole clip.
  If a line is missing from the main take, the first take that has it goes on V1.
  With `--enable-alts`, the alternates' audio plays together with V1's, so mute the tracks
  you don't want.
- **Pick best** scores each take by its worst-matching line in the clip (ties go to
  the earlier take), so one fluffed line costs a take the slot.
- The timeline's frame rate and size come from the first character's main take.

### Files Resolve can't read

Resolve (on macOS) reads .mov, .mp4, and .mxf files with H.264, HEVC, ProRes, or DNxHD/HR
video and AAC or PCM audio. Anything else is converted automatically. For example, an
`.mpeg` file with MP2 audio imports in Resolve without sound. The conversion uses ProRes
422 HQ (`prores_ks -profile:v 3`) and 16-bit PCM in a `.mov`:

- The copy is written next to the original as `<name>.prores.mov`. You can choose the
  workspace's `converted/` folder instead, and it's also the fallback when the original's
  folder isn't writable. An existing copy that's newer than the original is reused.
- The FCPXML points at the ProRes copy, so keep it.
- The transcript cache is shared: `take.mpeg.words.json` is reused for
  `take.prores.mov` (and the reverse), because the timing is identical.

## Changing a character's voice

autocut can replace a character's voice with an ElevenLabs voice using fal.ai's
[ElevenLabs Voice Changer](https://fal.ai/models/fal-ai/elevenlabs/voice-changer). It's a voice
*changer*, not text-to-speech: it keeps the actor's performance, timing, and delivery, so the cut
still lines up.

1. Get an API key at https://fal.ai/dashboard/keys. In the web UI, paste it under
   **Options › Voice** and click *Save key* (it's stored in `~/Movies/Autocut/settings.json`,
   readable only by you). On the command line, `export FAL_KEY=...` instead.
2. In the character's box under **Takes**, type or pick a voice: one of ElevenLabs' standard
   voices (Rachel, Aria, Brian, George, ...) or any voice ID that fal accepts. Leave it empty
   to keep the original voice.
3. Click **▶ Preview** to hear about 8 seconds of the main take in that voice (about $0.04).
4. Build the cut as usual.

What happens: after the cut is worked out, only the parts of that character's takes that the
timeline uses (V1 clips and stacked alternates, plus 0.4 s either side) are sent to fal.
The converted audio replaces the original at the same position, and the video is copied
unchanged into `<take>.<Voice>.mov` next to the take (or in the converted folder). The FCPXML
and preview use that file. Transcription and alignment still use the original audio.

- **Cost:** fal charges $0.30 per minute of audio sent. The log shows each take's cost.
  Converted stretches are cached in `~/Library/Caches/autocut/voice`, so re-running the same cut,
  or a cut whose clips haven't moved, doesn't pay again.
- **Outside the clips**, the voiced take still has the original audio. If you extend a clip in
  Resolve past its padding, you'll hear the original voice there.
- Only use a real person's voice with their permission; ElevenLabs' and fal's terms require it.

## Notes

- The screenplay PDF needs selectable text, in standard screenplay format (character
  cues around 3.5", dialogue around 2.5"). Use `--dump` / *Parse dialogue* to check it.
- PyTorch 2.6+ changed `torch.load` to `weights_only=True`, which breaks loading the
  voice-activity model bundled with WhisperX. autocut patches `torch.load` to
  `weights_only=False` for this load (`src/autocutlib/transcribe.py`). That model is a
  trusted file shipped with WhisperX.
- **Moving media after import:** the FCPXML has absolute `file://` paths. If you move the
  takes, relink them in Resolve or rebuild the cut.

## Development

```bash
pip install -e ".[dev]"      # pytest, reportlab (for test fixtures), ruff
pytest                       # runs without WhisperX: tests use synthetic transcripts
ruff check .
python tests/fixtures.py demo   # a demo screenplay + takes + cached transcripts to try the UI with
```

Layout: `src/autocutlib/` has `screenplay.py` (PDF → lines), `transcribe.py` (WhisperX +
cache), `align.py`, `edit.py` (segments, takes, pick-best), `fcpxml.py`, `media.py`
(ffprobe, conversion), `preview.py`, `voice.py` (fal.ai voice changer), `pipeline.py` (the whole job, shared by the CLI and the
web worker), and `cli.py`. `web/` has the Flask server, the job worker, and static files.
