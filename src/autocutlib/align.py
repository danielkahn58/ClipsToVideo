"""Map screenplay lines onto a take's transcript."""

from difflib import SequenceMatcher


def align(lines, ttoks):
    """Map each screenplay line to a span of transcript tokens. Returns a list of
    dicts (or None per line): first, last token index, and match ratio."""
    s_toks, owner = [], []
    for i, ln in enumerate(lines):
        for t in ln.tokens:
            s_toks.append(t)
            owner.append(i)

    sm = SequenceMatcher(None, s_toks, [t["tok"] for t in ttoks], autojunk=False)
    match = [None] * len(s_toks)
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            match[a + k] = b + k

    # For each script token: the last matched transcript index before it / first after it.
    prev_bound, running = [], -1
    for m in match:
        prev_bound.append(running)
        if m is not None:
            running = m
    next_bound, running = [0] * len(match), len(ttoks)
    for k in range(len(match) - 1, -1, -1):
        next_bound[k] = running
        if match[k] is not None:
            running = match[k]

    spans, k = [], 0
    for ln in lines:
        n = len(ln.tokens)
        idx = range(k, k + n)
        matched = [(j, match[j]) for j in idx if match[j] is not None]
        if not matched:
            spans.append(None)
        else:
            (j0, first), (j1, last) = matched[0], matched[-1]
            # Extend over leading/trailing words Whisper heard differently,
            # without crossing into neighbouring lines' matches.
            first = max(first - (j0 - k), prev_bound[k] + 1)
            last = min(last + (k + n - 1 - j1), next_bound[k + n - 1] - 1)
            spans.append({"first": first, "last": last, "ratio": len(matched) / n})
        k += n
    return spans
