"""WhisperX transcription with word timestamps, cached next to each video as <file>.words.json."""

import json
from pathlib import Path

from .report import AutocutError, Reporter
from .text import tokenize


def cache_path(video):
    return Path(str(video) + ".words.json")


def _patch_torch_load(torch):
    # PyTorch 2.6+ defaults torch.load(weights_only=True), which rejects the voice-activity
    # model that ships inside the WhisperX package. It's a trusted local file, so load it normally.
    if not getattr(torch.load, "_autocut_patched", False):
        _orig_load = torch.load

        def _load(*a, **k):
            k["weights_only"] = False
            return _orig_load(*a, **k)
        _load._autocut_patched = True
        torch.load = _load


class Transcriber:
    """Holds the Whisper/alignment models so several takes in one run load them only once."""

    def __init__(self, model="large-v3", language="en", retranscribe=False, reporter=None):
        self.model_name, self.language, self.retranscribe = model, language, retranscribe
        self.rep = reporter or Reporter()
        self._model = None
        self._align = {}

    def words(self, video, aliases=()):
        """Word list for `video`. `aliases` are other files with identical timing (the original
        a ProRes copy was converted from): a cache found on any of them is reused, and a fresh
        transcript is written to all of them."""
        files = [Path(video), *map(Path, aliases)]
        if not self.retranscribe:
            for f in files:
                c = cache_path(f)
                if c.exists():
                    words = json.loads(c.read_text())
                    self.rep.log(f"Using cached transcript {c.name}")
                    self._write_caches(words, files, skip_existing=True)
                    return words

        words = self._transcribe(files[0])
        self._write_caches(words, files)
        return words

    def _write_caches(self, words, files, skip_existing=False):
        for f in files:
            c = cache_path(f)
            if skip_existing and c.exists():
                continue
            try:
                c.write_text(json.dumps(words, indent=1))
            except OSError as e:
                self.rep.warn(f"Couldn't write transcript cache {c}: {e}")

    def _load(self):
        if self._model is not None:
            return
        try:
            import torch
            _patch_torch_load(torch)
            import whisperx
        except ImportError as e:
            raise AutocutError(f"WhisperX isn't installed ({e}), and there's no cached transcript for "
                               "this take. Install it with: pip install -e '.[whisper]'") from None
        self._torch, self._wx = torch, whisperx
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        compute = "float16" if self.device == "cuda" else "int8"
        self.rep.log(f"Loading Whisper model {self.model_name} on {self.device} "
                     "(first run downloads it)...")
        self._model = whisperx.load_model(self.model_name, self.device, compute_type=compute,
                                          language=self.language)

    def _transcribe(self, video):
        self.rep.log(f"Transcribing {video} ...")
        self._load()
        wx = self._wx
        audio = wx.load_audio(str(video))
        self.rep.progress(0.05, "transcribing")
        try:
            result = self._model.transcribe(audio, batch_size=8, print_progress=True)
        except TypeError:                       # older WhisperX without print_progress
            result = self._model.transcribe(audio, batch_size=8)
        self.rep.progress(0.7, "aligning words")
        lang = result.get("language", self.language)
        if lang not in self._align:
            self._align[lang] = wx.load_align_model(language_code=lang, device=self.device)
        align_model, meta = self._align[lang]
        result = wx.align(result["segments"], align_model, meta, audio, self.device,
                          return_char_alignments=False)
        self.rep.progress(1.0, "done")

        words = []
        for seg in result["segments"]:
            for w in seg.get("words", []):
                words.append({"word": w.get("word", ""), "start": w.get("start"), "end": w.get("end")})
        return fill_missing_times(words)


def fill_missing_times(words):
    """Words WhisperX couldn't time (numbers, symbols) get their neighbours' times."""
    for i, w in enumerate(words):
        if w["start"] is None:
            w["start"] = words[i - 1]["end"] if i > 0 and words[i - 1]["end"] is not None else 0.0
        if w["end"] is None:
            nxt = next((x["start"] for x in words[i + 1:] if x["start"] is not None), None)
            w["end"] = nxt if nxt is not None else w["start"] + 0.3
    return words


def transcript_tokens(words):
    toks = []
    for w in words:
        for t in tokenize(w["word"]):
            toks.append({"tok": t, "start": w["start"], "end": w["end"]})
    return toks
