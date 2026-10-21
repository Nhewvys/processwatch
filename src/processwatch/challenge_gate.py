"""Safe classification and stop policy for access challenges.

This module detects challenge families and releases collection workers by
returning a non-retryable access-pending decision. It intentionally has no
solver, token, cookie, stealth, or browser-session handling.
"""
from dataclasses import dataclass
from enum import StrEnum
import re


class ChallengeKind(StrEnum):
    NONE = "none"
    CAPTCHA = "captcha"
    RECAPTCHA = "recaptcha"
    HCAPTCHA = "hcaptcha"
    TURNSTILE = "turnstile"
    CLOUDFLARE = "cloudflare_challenge"
    UNKNOWN = "unknown_challenge"


@dataclass(frozen=True)
class ChallengeObservation:
    kind: ChallengeKind
    detected: bool
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChallengeDecision:
    status: str
    provider_id: str | None
    retryable: bool
    worker_released: bool
    reason: str


_RULES = (
    (ChallengeKind.RECAPTCHA, re.compile(r"recaptcha|g-recaptcha-response|google\.com/recaptcha", re.I)),
    (ChallengeKind.HCAPTCHA, re.compile(r"hcaptcha|h-captcha-response|hcaptcha\.com", re.I)),
    (ChallengeKind.TURNSTILE, re.compile(r"turnstile|cf-turnstile|challenges\.cloudflare\.com/turnstile", re.I)),
    (ChallengeKind.CLOUDFLARE, re.compile(r"cloudflare.{0,24}(challenge|verify)|checking your browser", re.I)),
    (ChallengeKind.CAPTCHA, re.compile(r"\bcaptcha\b|human verification|verify you are human", re.I)),
)


def classify_challenge(*signals: str | None) -> ChallengeObservation:
    """Classify short DOM labels/markers; never retains page content or URLs."""
    text = " ".join(str(signal or "")[:512] for signal in signals).casefold()
    if not text.strip():
        return ChallengeObservation(ChallengeKind.NONE, False)
    for kind, pattern in _RULES:
        match = pattern.search(text)
        if match:
            # Keep only the category, not raw page text, tokens, URLs, or labels.
            return ChallengeObservation(kind, True, (kind.value,))
    if re.search(r"challenge|verification required|verify", text, re.I):
        return ChallengeObservation(ChallengeKind.UNKNOWN, True, ("generic_challenge_marker",))
    return ChallengeObservation(ChallengeKind.NONE, False)


def decide_challenge_action(observation: ChallengeObservation) -> ChallengeDecision:
    """Stop safely on a challenge; no provider is invoked and no retry is queued."""
    if not observation.detected:
        return ChallengeDecision("continue", None, False, True, "no_access_challenge")
    return ChallengeDecision(
        status="awaiting_access", provider_id=None, retryable=False,
        worker_released=True,
        reason="A fonte exige verificação de acesso; a coleta foi pausada sem novas tentativas automáticas.",
    )
