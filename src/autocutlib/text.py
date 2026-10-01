import re


def tokenize(text):
    text = text.lower().replace("’", "'").replace("‘", "'")
    text = re.sub(r"[—–\-]+", " ", text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return [w.replace("'", "") for w in text.split() if w.replace("'", "")]


def fmt_tc(sec):
    return f"{int(sec // 60):02d}:{sec % 60:05.2f}"
