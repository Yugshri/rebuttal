"""Thin, swappable LLM wrapper — the *only* place the pipeline calls an LLM.

Scope, deliberately narrow: the single LLM-driven surface in this system is the
``explanation_letter`` evidence slot (a narrative summary of evidence the system
has *already* assembled from real records). Everything else — classification,
scoring, risk — is deterministic rule-based code on purpose, because "explainable,
bounded, gated" is what the track rubric rewards.

Provider: **Sarvam** (``sarvam-m``) — an India-sovereign model, which fits this
system's compliance-grounding narrative (data-residency, DPDP alignment), and has
a free tier. OpenAI-compatible request/response shape. Swapping providers means
changing only this file.

Degradation is silent and safe: if ``SARVAM_API_KEY`` is unset, the ``httpx``
call errors, auth fails (Sarvam returns HTTP 403 with ``invalid_api_key_error``
for a bad key — not 401), or the response is malformed, :func:`generate` returns
``None`` and the caller falls back to a deterministic template. Tests and the full
pipeline therefore run offline with no key.
"""

from __future__ import annotations

import os

import httpx

# Confirmed against docs.sarvam.ai (api-reference/chat/chat-completions): the
# chat-completions endpoint is OpenAI-shaped and lives at this URL.
SARVAM_CHAT_URL = os.environ.get(
    "TRACK02_LLM_URL", "https://api.sarvam.ai/v1/chat/completions"
)

# The user specified "sarvam-m" for this build. Overridable so a different Sarvam
# tier (e.g. sarvam-105b) can be selected without a code change.
DEFAULT_MODEL = os.environ.get("TRACK02_LLM_MODEL", "sarvam-m")

# Env var holding the key. Sarvam keys are "sk_..." and go in the
# `api-subscription-key` header (their primary convention; Bearer is also
# accepted). Never hardcode or commit the key.
API_KEY_ENV = "SARVAM_API_KEY"

# Low temperature: this is summarisation/narration over supplied facts, not a
# creative task. As close to deterministic as the provider allows.
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 800
DEFAULT_TIMEOUT_S = 30.0


def is_configured() -> bool:
    """True only if an LLM call could plausibly succeed (key present)."""
    return bool(os.environ.get(API_KEY_ENV))


def generate(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> str | None:
    """Return the model's text, or ``None`` if unavailable/failed.

    Callers MUST handle ``None`` by falling back to deterministic output. Never
    let a missing or flaky LLM break the pipeline.
    """
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        return None

    payload = {
        "model": model or DEFAULT_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "api-subscription-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(
            SARVAM_CHAT_URL, json=payload, headers=headers, timeout=timeout_s
        )
        # 403 == bad/invalid key for Sarvam (not 401). Any non-2xx -> fallback.
        if response.status_code >= 400:
            return None
        data = response.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return text or None
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        # network, timeout, auth, rate limit, schema drift -> deterministic
        # fallback. The caller logs the degradation; we do not raise.
        return None
