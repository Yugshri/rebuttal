"""Thin, swappable LLM wrapper — the *only* place the pipeline calls an LLM.

Scope, deliberately narrow: the single LLM-driven surface in this system is the
``explanation_letter`` evidence slot (a narrative summary of evidence the system
has *already* assembled from real records). Everything else — classification,
scoring, risk — is deterministic rule-based code on purpose, because "explainable,
bounded, gated" is what the track rubric rewards.

Provider: Groq (free tier, OpenAI-compatible). Swapping providers means changing
only this file. If ``GROQ_API_KEY`` is unset, or the call errors, or the ``groq``
package is missing, :func:`generate` returns ``None`` and the caller falls back to
a deterministic template — so tests and the full pipeline run offline with no key.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = os.environ.get("TRACK02_LLM_MODEL", "llama-3.3-70b-versatile")

# Low temperature: this is a summarisation/narration task over supplied facts, not
# a creative one. We want it as close to deterministic as the provider allows.
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 800


def is_configured() -> bool:
    """True only if an LLM call could actually succeed (key + package present)."""
    if not os.environ.get("GROQ_API_KEY"):
        return False
    try:
        import groq  # noqa: F401
    except ImportError:
        return False
    return True


def generate(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str | None:
    """Return the model's text, or ``None`` if unavailable/failed.

    Callers MUST handle ``None`` by falling back to deterministic output. Never
    let a missing or flaky LLM break the pipeline.
    """
    if not is_configured():
        return None
    try:
        from groq import Groq

        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        response = client.chat.completions.create(
            model=model or DEFAULT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        return text or None
    except Exception:
        # Any failure (network, auth, rate limit, schema change) -> deterministic
        # fallback. The caller logs the degradation; we do not raise.
        return None
