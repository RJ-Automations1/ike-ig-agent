"""Distill Dr. Ike's corrections into short, reusable writing lessons.

Every time he edits a draft or redoes one with a note, we ask Claude whether
the correction reveals a durable preference. If it does, the lesson is stored
and injected into all future generation prompts. Lessons are visible (and
deletable) on the dashboard.
"""
import logging

import anthropic

import config

log = logging.getLogger(__name__)

DISTILL_PROMPT = """Dr. Ike Ogbaa reviewed an AI-drafted social media post and corrected it.
Correction type: {kind}
Post category: {category}

{body}

Lessons already learned (do NOT repeat or rephrase any of these):
{existing}

If this correction reveals a DURABLE style or content preference that should
shape ALL future posts, state it as ONE short imperative rule, max 20 words.
Examples: "Open with a question rather than a statistic." / "Never use the
word 'game-changer'." / "Keep captions under 150 words."

If the correction is one-off/topical (a specific subject request), trivial
(typo, spacing), or already covered by an existing lesson, reply exactly NONE.

If it MIXES a durable preference with a one-off topic (e.g. "make it shorter
and about mentorship"), extract ONLY the durable part ("Keep posts shorter.")
and drop the topic — topics are handled separately and must not become rules.

Reply with the single rule or NONE — nothing else."""


def distill_lesson(kind: str, category: str, before: str = "",
                   after: str = "", note: str = "",
                   existing: list[str] | None = None) -> str | None:
    """Returns a lesson string, or None if the correction teaches nothing durable."""
    if kind == "edit":
        body = f"ORIGINAL DRAFT:\n{before}\n\nHIS EDITED VERSION:\n{after}"
    else:  # redo
        body = (f"HE DISCARDED THIS DRAFT:\n{before}\n\n"
                f"HIS REDO NOTE (what he asked for instead):\n{note}")

    existing_block = "\n".join(f"- {l}" for l in (existing or [])) or "(none yet)"
    prompt = DISTILL_PROMPT.format(kind=kind, category=category or "general",
                                   body=body, existing=existing_block)

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
    response = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text or text.upper().startswith("NONE") or len(text) > 200:
        return None
    return text.strip().strip('"')
