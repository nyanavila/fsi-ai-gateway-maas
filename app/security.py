"""
Security layer — three responsibilities:
  1. PII masking           — redact sensitive fields before leaving the network
  2. Injection detection   — block prompt hijacking attempts
  3. Sentiment analysis    — VADER-based score for escalation routing

VADER is a lexicon-based model running in-process with no API calls.
Install via: pip install vaderSentiment  (already in requirements.txt)
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


def _load_vader():
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
        logger.info("Sentiment backend: VADER (lexicon-based)")
        return analyzer
    except ImportError:
        logger.warning("vaderSentiment not installed — using keyword heuristic fallback")
        return None


_VADER_ANALYZER: Optional[object] = _load_vader()

# ── PII patterns (FSI UK/US, ordered most-specific first) ─────────────────────

PII_PATTERNS = [
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[CARD_NUMBER]"),
    (re.compile(r"\b[A-Z]{2}\d{6}[A-D]\b", re.IGNORECASE), "[NI_NUMBER]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b", re.IGNORECASE), "[IBAN]"),
    (re.compile(r"\b\d{2}-\d{2}-\d{2}\b"), "[SORT_CODE]"),
    (re.compile(r"(?<!\d)\d{8}(?!\d)"), "[ACCOUNT_NUMBER]"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\+44\s?|0)(?:\d\s?){9,10}\b"), "[PHONE]"),
    (re.compile(r"\b(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}\b"), "[DATE]"),
    (re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b", re.IGNORECASE), "[POSTCODE]"),
    (re.compile(r"(?<!\d)\d{9}(?!\d)"), "[PASSPORT]"),
    (re.compile(r"\b([A-Z][a-z]{1,20} ){1,3}[A-Z][a-z]{1,20}\b"), "[NAME]"),
]

# ── Injection patterns ────────────────────────────────────────────────────────

INJECTION_PATTERNS = [
    re.compile(r"ignore (all )?(previous|prior|above) instructions?", re.IGNORECASE),
    re.compile(r"forget (you are|your role|all|everything)", re.IGNORECASE),
    re.compile(r"you are now (dan|an? ai with no|unrestricted|free)", re.IGNORECASE),
    re.compile(r"(reveal|output|print|show|return) (your |the )?(system prompt|instructions|rules|config)", re.IGNORECASE),
    re.compile(r"(act|behave|pretend|roleplay|simulate) as (if you (have|are)|a (different|new|evil|unrestricted))", re.IGNORECASE),
    re.compile(r"\bjailbreak\b", re.IGNORECASE),
    re.compile(r"disregard (your|all|the) (guidelines|rules|policy|restrictions|training)", re.IGNORECASE),
    re.compile(r"(bypass|override|disable|circumvent) (safety|content|security|filter|guardrail)", re.IGNORECASE),
    re.compile(r"<\s*(script|iframe|object|embed|img\s+src)", re.IGNORECASE),
    re.compile(r"(\{|\[)\s*\"?role\"?\s*:", re.IGNORECASE),
    re.compile(r"(do anything now|dan mode|developer mode)", re.IGNORECASE),
    re.compile(r"prompt\s*(leak|inject|hack|exploit)", re.IGNORECASE),
]

_NEG_WORDS = {
    "furious", "angry", "disgusted", "terrible", "horrible", "awful",
    "unacceptable", "outrageous", "scam", "fraud", "stolen", "worst",
    "useless", "incompetent", "disgusting", "pathetic", "liar", "lied",
}
_POS_WORDS = {
    "thanks", "great", "excellent", "helpful", "happy",
    "pleased", "satisfied", "perfect", "wonderful", "appreciate",
}


@dataclass
class InjectionResult:
    blocked: bool
    score: float
    matched_patterns: list[str] = field(default_factory=list)


class SecurityLayer:

    def mask_pii(self, text: str) -> tuple[str, list[str]]:
        """Replace PII values with labelled placeholders."""
        masked = text
        found: list[str] = []
        for pattern, placeholder in PII_PATTERNS:
            new_text, count = pattern.subn(placeholder, masked)
            if count > 0:
                if placeholder not in found:
                    found.append(placeholder)
                masked = new_text
        return masked, found

    def check_injection(self, text: str) -> InjectionResult:
        """Screen for prompt injection / jailbreak attempts."""
        matched = [p.pattern for p in INJECTION_PATTERNS if p.search(text)]
        score = round(len(matched) / len(INJECTION_PATTERNS), 4)
        blocked = len(matched) > 0
        if blocked:
            logger.warning("Injection attempt detected", extra={
                "patterns_matched": len(matched),
                "score": score,
            })
        return InjectionResult(blocked=blocked, score=score, matched_patterns=matched)

    def analyze_sentiment(self, text: str) -> float:
        """
        Compound sentiment score in [-1.0, 1.0].
        Uses VADER when available; falls back to keyword heuristic.
        Score below -0.5 is a strong escalation signal.
        """
        if _VADER_ANALYZER is not None:
            scores = _VADER_ANALYZER.polarity_scores(text)
            return round(scores["compound"], 3)
        words = set(text.lower().split())
        neg = len(words & _NEG_WORDS)
        pos = len(words & _POS_WORDS)
        total = neg + pos
        if total == 0:
            return 0.0
        return round((pos - neg) / total, 3)

    def scan_output(self, text: str) -> tuple[str, list[str]]:
        """Scan model output for accidental PII leakage (DLP)."""
        return self.mask_pii(text)
