"""Queryable reason-code classification table.

This is a *view* over ``src/common/reason_codes.py`` — never a parallel copy. The
router exposes it (via ``api.py``) so ``evidence-assembler`` can ask "for this
dispute's category, which evidence types / EvidenceBundle slots matter?" without
re-deriving the mapping.

An unrecognised network code is routed to
:data:`~src.common.reason_codes.NEEDS_MANUAL_CLASSIFICATION` explicitly — it is
never guessed into the nearest category.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.common import reason_codes as rc


@dataclass(frozen=True)
class ClassificationRow:
    """One row of the queryable table: a known ``<network>:<code>`` mapping."""

    network: str
    code: str
    key: str
    category: str
    evidence_types: tuple[str, ...]
    required_slots: tuple[str, ...]


@dataclass(frozen=True)
class ClassificationResult:
    """Outcome of classifying a single dispute's reason code."""

    network: str
    code: str
    key: str
    category: str
    needs_manual_classification: bool
    reason: str
    evidence_types: tuple[str, ...] = field(default_factory=tuple)
    required_slots: tuple[str, ...] = field(default_factory=tuple)


def _row_for_key(key: str) -> ClassificationRow:
    network, _, code = key.partition(":")
    category = rc.REASON_CODE_TO_CATEGORY[key]
    return ClassificationRow(
        network=network,
        code=code,
        key=key,
        category=category,
        evidence_types=tuple(rc.CATEGORY_EVIDENCE_TYPES.get(category, ())),
        required_slots=tuple(rc.CATEGORY_REQUIRED_SLOTS.get(category, ())),
    )


# Built once, at import, straight from the shared constants.
CLASSIFICATION_TABLE: dict[str, ClassificationRow] = {
    key: _row_for_key(key) for key in sorted(rc.REASON_CODE_TO_CATEGORY)
}


def classification_table() -> list[ClassificationRow]:
    """The full known mapping, deterministically ordered by ``network:code``."""
    return [CLASSIFICATION_TABLE[k] for k in sorted(CLASSIFICATION_TABLE)]


def evidence_types_for_category(category: str) -> tuple[str, ...]:
    return tuple(rc.CATEGORY_EVIDENCE_TYPES.get(category, ()))


def required_slots_for_category(category: str) -> tuple[str, ...]:
    return tuple(rc.CATEGORY_REQUIRED_SLOTS.get(category, ()))


def classify_reason_code(network: str | None, code: str | None) -> ClassificationResult:
    """Classify one dispute's ``(network, reason_code)`` pair.

    Returns a :class:`ClassificationResult`; ``needs_manual_classification`` is
    ``True`` (with a human-readable ``reason``) when the network is missing or the
    code is a real network code our table does not map yet.
    """
    norm_net = (network or "").strip().lower()
    norm_code = (code or "").strip()

    if not norm_net or not norm_code:
        return ClassificationResult(
            network=norm_net,
            code=norm_code,
            key=f"{norm_net}:{norm_code}",
            category=rc.NEEDS_MANUAL_CLASSIFICATION,
            needs_manual_classification=True,
            reason=(
                "missing network or reason_code on the webhook payload — cannot "
                "look up a category"
            ),
        )

    key = f"{norm_net}:{norm_code}"
    category = rc.classify(norm_net, norm_code)

    if category == rc.NEEDS_MANUAL_CLASSIFICATION:
        return ClassificationResult(
            network=norm_net,
            code=norm_code,
            key=key,
            category=category,
            needs_manual_classification=True,
            reason=(
                f"reason code {norm_net}:{norm_code} is a real network code that "
                "the shared reason-code table does not map to a category yet — "
                "routed to manual classification rather than guessed"
            ),
        )

    return ClassificationResult(
        network=norm_net,
        code=norm_code,
        key=key,
        category=category,
        needs_manual_classification=False,
        reason="matched a known entry in the shared reason-code table",
        evidence_types=evidence_types_for_category(category),
        required_slots=required_slots_for_category(category),
    )
