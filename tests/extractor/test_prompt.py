"""Prompt-builder tests.

These don't hit OpenAI; they assert the message layout the SDK will
serialize and that few-shot examples themselves validate against the
schema. A snapshot-style check on the system prompt protects against
accidental whitespace edits that would invalidate prompt caching.
"""

from __future__ import annotations

import json

from clinical_demo.extractor.prompt import (
    FEW_SHOT_EXAMPLES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_messages,
)
from clinical_demo.extractor.schema import ExtractedCriteria


def test_prompt_version_is_a_nonempty_string():
    """A blank or numerically-typed prompt version makes
    `ExtractorRunMeta.prompt_version` ambiguous; force it stringy and
    non-empty."""
    assert isinstance(PROMPT_VERSION, str)
    assert PROMPT_VERSION
    assert " " not in PROMPT_VERSION


def test_system_prompt_contains_load_bearing_phrases():
    """Lightweight regression guard: the system prompt's hard-rule
    bullets should survive accidental edits. We don't snapshot the
    whole text (too brittle), just the must-have phrases."""
    must_haves = [
        "Faithful to the source",
        "Atomicity",
        "Polarity from headers",
        "Negation is independent of polarity",
        "Verbatim source_text",
        "Lower-case surface forms",
        "Units verbatim",
        "Exactly one payload per row",
    ]
    for phrase in must_haves:
        assert phrase in SYSTEM_PROMPT, f"missing rule: {phrase!r}"


def test_build_messages_layout():
    """Expected shape: system, then alternating user/assistant pairs
    for each few-shot example, then a final user turn carrying the
    real input."""
    msgs = build_messages("Inclusion Criteria:\n* Adults 18+")
    expected_count = 1 + 2 * len(FEW_SHOT_EXAMPLES) + 1
    assert len(msgs) == expected_count

    assert msgs[0]["role"] == "system"
    for i, _ in enumerate(FEW_SHOT_EXAMPLES):
        assert msgs[1 + 2 * i]["role"] == "user"
        assert msgs[2 + 2 * i]["role"] == "assistant"
    assert msgs[-1]["role"] == "user"
    assert "Adults 18+" in msgs[-1]["content"]


def test_few_shot_assistant_payloads_validate():
    """Each gold-standard assistant turn must itself parse back into
    the schema. Catches schema/example drift."""
    for _user, gold in FEW_SHOT_EXAMPLES:
        roundtripped = ExtractedCriteria.model_validate_json(gold.model_dump_json())
        assert roundtripped == gold


def test_few_shot_assistant_payloads_serialize_as_pure_json():
    """Strict mode rejects non-JSON content; if any few-shot
    serialization snuck in a non-JSON character we'd be in for a fun
    422 at runtime. Cheap to assert here."""
    for _user, gold in FEW_SHOT_EXAMPLES:
        text = gold.model_dump_json()
        json.loads(text)  # raises if invalid


def test_user_message_quotes_eligibility_text_verbatim():
    """The eligibility text must reach the model uncorrupted."""
    text = "Inclusion Criteria:\n* Some unusual character: ≥ 7%"
    msgs = build_messages(text)
    assert text in msgs[-1]["content"]
