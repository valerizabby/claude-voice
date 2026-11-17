from __future__ import annotations

import re

from .config import HALLUCINATIONS, STOP_WORDS, WAKE_FOLLOWUP_SEC, WAKE_WORDS, YES_WORDS


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def is_hallucination(text: str) -> bool:
    n = normalize(text)
    return any(h in n for h in HALLUCINATIONS)


def split_wake(text: str) -> str | None:
    words = normalize(text).split()
    if not words or words[0] not in WAKE_WORDS:
        return None
    return " ".join(words[1:])


def collect_commands(utts: list[tuple[float, str]],
                     followup: tuple[float, float] | None = None) -> list[tuple[str, str]]:
    """followup=(start, end): a phrase that ended inside it counts without the wake word."""
    utts = sorted(utts, key=lambda u: u[0])
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(utts):
        ts, text = utts[i]
        i += 1
        if is_hallucination(text):
            continue
        rest = split_wake(text)
        if rest is None:
            if not (followup and followup[0] <= ts <= followup[1]):
                continue
            rest = normalize(text)
        if rest == "" and i < len(utts):
            nts, ntext = utts[i]
            # a follow-up with its own wake word is handled by the next iteration
            if nts - ts <= WAKE_FOLLOWUP_SEC and not is_hallucination(ntext) and split_wake(ntext) is None:
                rest = normalize(ntext)
                i += 1
        if rest == "":
            continue
        if all(w in STOP_WORDS for w in rest.split()):
            out.append(("stop", ""))
        else:
            out.append(("command", rest))
    return out


def yes_words(text: str) -> bool:
    words = normalize(text).split()
    return bool(words) and all(w in YES_WORDS for w in words)


def parse_yes(text: str) -> bool:
    """«Клод, да»: the wake word is required, so a yes from the TV or another voice does not count."""
    rest = split_wake(text)
    return rest is not None and yes_words(rest)
