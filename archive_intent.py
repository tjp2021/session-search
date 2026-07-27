"""Pure, conservative classification of session archive and resume intent."""

from __future__ import annotations

import dataclasses
import enum
import re
import unicodedata


PARSER_VERSION = 4
MAX_INTENT_CHARS = 65_536
SUSPICIOUS_CONTROL_RE = re.compile(r"[\x00\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


class IntentKind(str, enum.Enum):
    CLOSE = "close"
    RESUME = "resume"
    NONE = "none"


@dataclasses.dataclass(frozen=True)
class IntentDecision:
    kind: IntentKind
    reason: str
    matched_clause: str = ""
    parser_version: int = PARSER_VERSION


QUOTE_RE = re.compile(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|`[^`\n]*`')
WORD_RE = re.compile(r"[a-z0-9']+")
CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|[;\n]+")

CLOSE_WORDS = {
    "archive", "cease", "close", "conclude", "dismiss", "end", "finalize", "finish",
    "retire", "seal", "shutdown", "terminate", "wrap",
}
RESUME_WORDS = {
    "continue", "reactivate", "reinstate", "reopen", "restart", "resume", "revive",
    "unarchive", "unseal",
}
CURRENT_WORDS = {
    "chat", "conversation", "context", "convo", "dialogue", "discussion", "exchange",
    "session", "thread",
}
TYPO_VOCAB = CLOSE_WORDS | RESUME_WORDS | CURRENT_WORDS | {"summarize"}
INTENT_ACTION_RE = re.compile(
    r"\b(?:archive|cease|close|closed|conclude|dismiss|end|finalize|finish|finished|"
    r"mark|retire|seal|terminate|wrap|shut|done|file|declare|"
    r"resume|reactivate|reinstate|reopen|restart|revive|unarchive|unseal|continue|"
    r"bring|carry|open|pick|restore|return|start|take|wake|"
    r"arhive|arvhive|clsoe|resuem)\b",
    re.I,
)
INTENT_REFERENT_RE = re.compile(
    r"\b(?:chat|session|thread|conversation|context|convo|dialogue|discussion|exchange|"
    r"sesion|sessoin|sesson)\b|"
    r"\bclose\s+(?:this|it)\s+(?:out|down)\b",
    re.I,
)

NEGATION_RE = re.compile(r"\b(?:do\s+not|don't|dont|never|no|not)\b", re.I)
QUESTION_RE = re.compile(r"^\s*(?:should|can|could|would|will|do|did|is|are|what|when|why|how)\b", re.I)
CONDITIONAL_RE = re.compile(r"\b(?:if|unless|when|whenever)\b", re.I)
FUTURE_RE = re.compile(
    r"\b(?:later|tomorrow|next\s+(?:week|month|time)|not\s+yet|once|plan\s+to|will|after|"
    r"before\s+(?:we|you|this)|eventually)\b",
    re.I,
)
META_RE = re.compile(
    r"\b(?:phrase|command|detector|classifier|feature|example|explain|recognize|test|means|"
    r"if\s+i\s+say|when\s+i\s+say|almost\s+said)\b",
    re.I,
)
CONTINUE_TAIL_RE = re.compile(
    r"\b(?:but|then|and)\b.{0,45}\b(?:continue|keep\s+going|keep\b.{0,25}\bopen|"
    r"carry\s+on|not\s+yet)\b",
    re.I,
)
WRONG_REFERENT_RE = re.compile(r"\b(?:plan|issue|task|ticket|client|interview|campaign|file|document)\b", re.I)
PASTED_CONTEXT_RE = re.compile(
    r"```|^\s*>|^\s*(?:user|assistant|claude|codex)\s*:|"
    r"^\s*(?:he|she|they)\s+(?:wrote|said)\s*:|^\s*copied\s+command\s*:|"
    r"^\s*email\s+from\b.*:|"
    r"\b(?:here\s+is\s+the\s+command|the\s+user\s+requested|example|transcript)\s*:",
    re.I | re.M,
)
WRONG_OBJECT_RE = re.compile(
    r"\b(?:archive|close|end|finish)\s+(?:the\s+)?"
    r"(?:issue|task|ticket|file|document|notes?|summary|analysis|report|plan|output|transcript)\b|"
    r"\bmark\s+(?:the\s+)?(?:issue|task|ticket|file|document|plan)\s+clos(?:e|ed)\b|"
    r"\bclose\s+(?:the\s+)?browser\s+tab\b|"
    r"\bfinish\s+(?:analyzing|debugging|reading|reviewing|writing|summarizing)\s+"
    r"(?:this|the|current)\s+(?:session|thread|conversation|context)\b|"
    r"\b(?:archive|close|end|finish)\s+(?:this|the|current)\s+"
    r"(?:session|thread|conversation|context)\s+"
    r"(?:summary|document|file|notes?|analysis|report|plan|output|transcript)\b|"
    r"\b(?:archive|close|end|finish)\s+(?:this|the|current)\s+chat\s+"
    r"(?:summary|document|file|notes?|analysis|report|plan|output|transcript)\b",
    re.I,
)
TERMINAL_REINFORCEMENT_RE = re.compile(
    r"(?:\band\s+)?\bdo\s+not\s+continue\s+it\b|"
    r"(?:\band\s+)?\bdon'?t\s+reopen\s+it\b|"
    r"(?:\band\s+)?\bbe\s+done\s+with\s+it\b|"
    r"(?:[—-]\s*)?\bno\s+more\s+repl(?:y|ies)\b",
    re.I,
)
DIRECTIVE_PREFIX_RE = re.compile(
    r"(?:^|,\s*)(?:ok(?:ay)?[,.]?\s+|please\s+(?:permanently\s+)?|now\s+|"
    r"permanently\s+|i(?:'m|\s+am)\s+all\s+set[—,-]?\s*|"
    r"we(?:'re|\s+are)\s+finished,?\s+(?:so\s+)?|"
    r"let'?s\s+|we\s+can\s+|we\s+are\s+finished\s+here[—,-]?\s*|"
    r"stop\s+here\s+and\s+|finish\s+and\s+|"
    r"name\s+and\s+summarize.{0,50}?\b(?:then|and)\s+)?"
    r"(?:archive|bring|cease|close|conclude|consider|declare|dismiss|end|file|finalize|"
    r"finish|kill|mark|put|retire|seal|shut|terminate|wrap)",
    re.I,
)
DONE_CURRENT_RE = re.compile(
    r"\b(?:we(?:'re|\s+are)\s+done\s+(?:with\s+)?(?:this|the|current)\s+"
    r"(?:session|thread|conversation|context)|(?:this|the|current)\s+"
    r"(?:session|thread|conversation|context)\s+is\s+(?:done|finished|closed))\b",
    re.I,
)


def _typo_variants(word: str) -> set[str]:
    letters = "abcdefghijklmnopqrstuvwxyz"
    variants = {word}
    variants.update(word[:i] + word[i + 1 :] for i in range(len(word)))
    variants.update(
        word[:i] + word[i + 1] + word[i] + word[i + 2 :]
        for i in range(len(word) - 1)
    )
    variants.update(
        word[:i] + letter + word[i + 1 :]
        for i in range(len(word))
        for letter in letters
    )
    variants.update(
        word[:i] + letter + word[i:]
        for i in range(len(word) + 1)
        for letter in letters
    )
    return variants


def _build_typo_map() -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for word in TYPO_VOCAB:
        for variant in _typo_variants(word):
            candidates.setdefault(variant, set()).add(word)
    return {
        variant: next(iter(matches))
        for variant, matches in candidates.items()
        if len(matches) == 1
    }


TYPO_MAP = _build_typo_map()
TYPO_MAP.update({
    "curent": "current",
    "reusme": "resume",
})
ACTION_TYPO_TOKENS = {
    token
    for token, word in TYPO_MAP.items()
    if word in CLOSE_WORDS | RESUME_WORDS
}
REFERENT_TYPO_TOKENS = {
    token
    for token, word in TYPO_MAP.items()
    if word in CURRENT_WORDS
}


def _normalize_typos(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0).lower()
        if token in TYPO_VOCAB or token in {"archived", "closed", "finished"} or len(token) < 4:
            return token
        return TYPO_MAP.get(token, token)

    return WORD_RE.sub(replace, text.lower())


def _strip_quoted_text(text: str) -> tuple[str, bool]:
    found = False

    def blank(match: re.Match[str]) -> str:
        nonlocal found
        found = True
        return " " * len(match.group(0))

    return QUOTE_RE.sub(blank, text), found


def _has_current_referent(clause: str) -> bool:
    noun = r"(?:chat|session|thread|conversation|context|convo|dialogue|discussion|exchange)"
    modifier = r"(?:(?:active|archived|current|exact|live|ongoing|paused|present|same|very|whole)\s+)?"
    return bool(
        re.search(rf"\b(?:this|the|our)\s+{modifier}{noun}\b", clause)
        or re.search(
            rf"\b(?:active|archived|current|exact|live|ongoing|paused|present|same|very|whole)\s+"
            rf"{noun}\b",
            clause,
        )
        or re.search(
            rf"\b{noun}\s+(?:i(?:'m|\s+am)|we(?:'re|\s+are))\s+"
            r"(?:in|using|speaking\s+in)\b",
            clause,
        )
        or re.search(rf"\b{noun}\s+(?:open\s+)?between\s+us\b", clause)
        or re.search(rf"\b{noun}\s+(?:on\s+screen|currently\s+before\s+us)\b", clause)
    )


def _has_close_action(clause: str) -> bool:
    clause = re.sub(r"\bun-?archive\b", "", clause)
    clause = re.sub(r"\b(?:out\s+of|back\s+from)\s+(?:the\s+)?archive\b", "", clause)
    return bool(
        re.search(
            r"\b(?:archive|cease|close|conclude|dismiss|end|finalize|finish|kill|retire|"
            r"terminate)\b",
            clause,
        )
        or re.search(r"\bfile\s+away\b", clause)
        or re.search(r"\bput\b.{0,55}\binto\s+the\s+archive\b", clause)
        or re.search(r"\bbring\b.{0,55}\bto\s+a\s+final\s+close\b", clause)
        or re.search(r"\bdeclare\b.{0,55}\bfinished\b", clause)
        or re.search(r"\bput\s+an?\s+end\s+to\b", clause)
        or re.search(
            r"\bconsider\s+.+\b(?:chat|session|thread|conversation|context)\s+"
            r"clos(?:e|ed)\b",
            clause,
        )
        or re.search(
            r"\bmark\s+(?:(?:this|the|current|our|present|same)\s+)?"
            r"(?:chat|session|thread|conversation|context)\s+closed\b",
            clause,
        )
        or re.search(
            r"\bseal\s+(?:(?:this|the|current|our|present|same)\s+)?"
            r"(?:(?:chat|session|thread|conversation|context)\s+)?up\b",
            clause,
        )
        or re.search(
            r"\bwrap\s+(?:(?:this|the|current|our|present|same)\s+)?"
            r"(?:(?:chat|session|thread|conversation|context)\s+)?up\b",
            clause,
        )
        or re.search(
            r"\bshut(?:\s+down)?\s+(?:(?:this|the|our)\s+"
            r"(?:(?:active|current|ongoing|present|same)\s+)?|"
            r"(?:active|current|ongoing|present|same)\s+)"
            r"(?:chat|session|thread|conversation|context)(?:\s+down)?\b",
            clause,
        )
        or re.search(r"\bclose\s+(?:this|it)\s+(?:out|down)\b", clause)
    )


def _has_resume_action(clause: str) -> bool:
    return bool(
        re.search(
            r"\b(?:resume|reactivate|reinstate|re-?open|continue|revive|un-?archive|"
            r"unseal)\b",
            clause,
        )
        or re.search(r"\bpick\b.{0,45}\b(?:back\s+up|up\b.{0,20}\bagain)\b", clause)
        or re.search(r"\bcarry\s+on\s+with\b", clause)
        or re.search(r"\brestart\b.{0,45}\b(?:and\s+)?continue\b", clause)
        or re.search(r"\bbring\b.{0,45}\bback\b.{0,25}\bproceed\b", clause)
        or re.search(r"\bopen\b.{0,45}\bback\s+up\b", clause)
        or re.search(r"\b(?:bring|take)\b.{0,55}\b(?:out\s+of\s+the\s+archive|live\s+again)\b", clause)
        or re.search(r"\breturn\b.{0,55}\b(?:proceed|open\s+state)\b", clause)
        or re.search(r"\bstart\b.{0,55}\bmoving\s+again\b", clause)
        or re.search(r"\bcarry\s+forward\b", clause)
        or re.search(r"\bwake\b.{0,45}\bback\s+up\b", clause)
        or re.search(r"\brestore\b.{0,55}\bgo\s+on\b", clause)
        or re.search(r"\bbring\b.{0,45}\bback\s+from\s+(?:the\s+)?archive\b", clause)
        or re.search(r"\bget\b.{0,45}\bgoing\s+again\b", clause)
    )


def _classify_clause(raw_clause: str) -> IntentDecision | None:
    clause = _normalize_typos(raw_clause.strip())
    safety_clause = TERMINAL_REINFORCEMENT_RE.sub("", clause)
    close_action = _has_close_action(clause) or bool(DONE_CURRENT_RE.search(clause))
    resume_action = _has_resume_action(safety_clause)
    if not close_action and not resume_action:
        return None
    if close_action and resume_action:
        return IntentDecision(IntentKind.NONE, "conflicting-intent", raw_clause.strip())
    if WRONG_OBJECT_RE.search(clause):
        return IntentDecision(IntentKind.NONE, "wrong-object", raw_clause.strip())
    if raw_clause.rstrip().endswith("?") or QUESTION_RE.search(clause):
        return IntentDecision(IntentKind.NONE, "question", raw_clause.strip())
    if NEGATION_RE.search(safety_clause):
        return IntentDecision(IntentKind.NONE, "negated", raw_clause.strip())
    if CONDITIONAL_RE.search(clause):
        return IntentDecision(IntentKind.NONE, "conditional", raw_clause.strip())
    if FUTURE_RE.search(clause):
        return IntentDecision(IntentKind.NONE, "future", raw_clause.strip())
    if META_RE.search(clause):
        return IntentDecision(IntentKind.NONE, "meta", raw_clause.strip())
    if close_action and CONTINUE_TAIL_RE.search(safety_clause):
        return IntentDecision(IntentKind.NONE, "continued-work-tail", raw_clause.strip())

    current = _has_current_referent(clause)
    close_shorthand = bool(re.search(r"\bclose\s+(?:this|it)\s+(?:out|down)\b", clause))
    summarize_close = bool(re.search(r"\bname\s+and\s+summarize\b", clause) and close_action)
    if close_action:
        if WRONG_REFERENT_RE.search(clause) and not current and not close_shorthand:
            return IntentDecision(IntentKind.NONE, "wrong-referent", raw_clause.strip())
        if not (current or close_shorthand or summarize_close or DONE_CURRENT_RE.search(clause)):
            return IntentDecision(IntentKind.NONE, "missing-current-session", raw_clause.strip())
        if not (DIRECTIVE_PREFIX_RE.search(clause) or summarize_close or close_shorthand or DONE_CURRENT_RE.search(clause)):
            return IntentDecision(IntentKind.NONE, "not-terminal-directive", raw_clause.strip())
        return IntentDecision(IntentKind.CLOSE, "explicit-terminal-close", raw_clause.strip())

    if resume_action:
        if not current:
            return IntentDecision(IntentKind.NONE, "missing-current-session", raw_clause.strip())
        if not re.search(
            r"^\s*(?:ok(?:ay)?[,.]?\s+|please\s+|now\s+|let'?s\s+)?"
            r"(?:resume|re-?open|continue|un-?archive|pick|carry\s+on|restart|"
            r"bring|carry\s+forward|get|open|reactivate|reinstate|restore|return|revive|"
            r"start|take|unseal|wake)\b",
            clause,
        ):
            return IntentDecision(IntentKind.NONE, "not-resume-directive", raw_clause.strip())
        return IntentDecision(IntentKind.RESUME, "explicit-resume", raw_clause.strip())
    return None


def classify_session_intent(text: str) -> IntentDecision:
    normalized = unicodedata.normalize("NFKC", text or "").strip()
    if not normalized:
        return IntentDecision(IntentKind.NONE, "empty")
    if len(normalized) > MAX_INTENT_CHARS:
        return IntentDecision(IntentKind.NONE, "oversized")
    if SUSPICIOUS_CONTROL_RE.search(normalized):
        return IntentDecision(IntentKind.NONE, "suspicious-control")
    if (
        normalized.count("`") % 2
        or normalized.count('"') % 2
        or normalized.count("“") != normalized.count("”")
    ):
        return IntentDecision(IntentKind.NONE, "unclosed-quotation")
    if PASTED_CONTEXT_RE.search(normalized):
        return IntentDecision(IntentKind.NONE, "pasted-or-attributed")
    visible, had_quotes = _strip_quoted_text(normalized)
    decisions: list[IntentDecision] = []
    for clause in CLAUSE_SPLIT_RE.split(visible):
        decision = _classify_clause(clause)
        if decision is not None:
            decisions.append(decision)
    if decisions:
        positive = [decision for decision in decisions if decision.kind is not IntentKind.NONE]
        if len(positive) != 1 or len(decisions) != 1:
            return IntentDecision(IntentKind.NONE, "ambiguous-multiple-clauses")
        return positive[0]
    if had_quotes and re.search(r"\b(?:archive|close|resume|reopen)\b", normalized, re.I):
        return IntentDecision(IntentKind.NONE, "quoted-only")
    return IntentDecision(IntentKind.NONE, "no-explicit-intent")


def has_session_intent_candidate(text: str) -> bool:
    clean = unicodedata.normalize("NFKC", text or "").lower()
    safety_clean = TERMINAL_REINFORCEMENT_RE.sub("", clean)
    if (
        PASTED_CONTEXT_RE.search(clean)
        or WRONG_OBJECT_RE.search(clean)
        or NEGATION_RE.search(safety_clean)
        or QUESTION_RE.search(clean)
        or CONDITIONAL_RE.search(clean)
        or FUTURE_RE.search(clean)
        or META_RE.search(clean)
        or (CONTINUE_TAIL_RE.search(safety_clean) and _has_close_action(_normalize_typos(safety_clean)))
    ):
        return False
    normalized = _normalize_typos(clean)
    tokens = WORD_RE.findall(normalized)
    referent = bool(INTENT_REFERENT_RE.search(normalized)) or _has_current_referent(normalized) or any(
        token in REFERENT_TYPO_TOKENS for token in tokens
    )
    shorthand = bool(
        re.search(r"\b(?:this|it)\b.{0,12}\b(?:out|down)\b", normalized)
        or re.search(r"\bname\b.{0,20}\bsummar\w*\b", normalized)
    )
    if not (referent or shorthand):
        return False
    return bool(INTENT_ACTION_RE.search(normalized)) or _has_close_action(normalized) or _has_resume_action(normalized) or any(
        token in ACTION_TYPO_TOKENS for token in tokens
    )
