"""Deterministic quality gates for rendered session-card fields."""

from __future__ import annotations

import dataclasses
import html
import re
import unicodedata


NO_CLEAR_STATE = "No clear completed work found in local evidence."
NO_CLEAR_RESUME = "No clear next step found in local evidence."

_PREFIX_RE = re.compile(r"^\s*(?:about|state|resume|next|context)\s*:\s*", re.I)
_TRUNCATED_RE = re.compile(r"(?:\.\.\.|…)\s*$")
_KNOWN_NOISE_RE = re.compile(r"\baftrer\b", re.I)
_SPACE_RE = re.compile(r"\s+")


@dataclasses.dataclass(frozen=True)
class CardFields:
    about: str
    state: str
    resume: str


def _balanced_prefix(text: str, opener: str, closer: str) -> str:
    if text.count(opener) > text.count(closer):
        text = text.split(opener, 1)[0].rstrip()
    return text


def clean_card_field(value: str, limit: int = 260) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(value or ""))
    text = "".join(char for char in text if char in "\n\t" or not unicodedata.category(char).startswith("C"))
    text = _PREFIX_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text).strip()
    quote_pairs = (('"', '"'), ("'", "'"), ("`", "`"), ("“", "”"), ("‘", "’"))
    for opener, closer in quote_pairs:
        if text.startswith(opener) and text.endswith(closer) and len(text) > 1:
            text = text[len(opener) : -len(closer)].strip()
            break
    text = text.lstrip("\"'`“”‘’ ").strip()
    if not text or _KNOWN_NOISE_RE.search(text) or _TRUNCATED_RE.search(text):
        return ""
    text = _balanced_prefix(text, "(", ")")
    text = _balanced_prefix(text, "[", "]")
    text = _balanced_prefix(text, "{", "}")
    text = text.strip(" -,:;")
    if not text:
        return ""
    if len(text) > limit:
        candidate = text[: limit + 1]
        boundary = max(candidate.rfind("."), candidate.rfind("!"), candidate.rfind("?"))
        if boundary >= max(40, limit // 2):
            text = candidate[: boundary + 1]
        else:
            word_boundary = candidate.rfind(" ")
            text = candidate[:word_boundary].rstrip() if word_boundary > 0 else ""
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def _same(left: str, right: str) -> bool:
    normalize = lambda value: re.sub(r"\W+", "", value).lower()
    normalized_left = normalize(left)
    normalized_right = normalize(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    shorter, longer = sorted((normalized_left, normalized_right), key=len)
    return len(shorter) >= 12 and shorter in longer


def quality_gate(about: str, state: str, resume: str) -> CardFields:
    clean_about = clean_card_field(about, 180) or "Untitled session."
    clean_state = clean_card_field(state, 240)
    clean_resume = clean_card_field(resume, 260)
    if not clean_state or _same(clean_about, clean_state):
        clean_state = NO_CLEAR_STATE
    if (
        not clean_resume
        or _same(clean_about, clean_resume)
        or _same(clean_state, clean_resume)
    ):
        clean_resume = NO_CLEAR_RESUME
    return CardFields(clean_about, clean_state, clean_resume)
