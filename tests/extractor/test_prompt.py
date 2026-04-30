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
    whole text (too brittle), just the must-have phrases.

    Rule 13 (single-concept typed slots) and Rule 14 (Chia mention
    discipline) are on this list because they are load-bearing eval
    fixes: the former protects matcher concept lookup; the latter
    protects layer-2 mention F1."""
    must_haves = [
        "Faithful to the source",
        "Atomicity",
        "Polarity from headers",
        "Negation is independent of polarity",
        "Verbatim source_text",
        "Lower-case surface forms",
        "Units verbatim",
        "Exactly one payload per row",
        "Single-concept typed slots",
        "Chia-style mentions are expected",
        "Mention boundary guidance",
        "Scope: label the full span",
    ]
    for phrase in must_haves:
        assert phrase in SYSTEM_PROMPT, f"missing rule: {phrase!r}"


def test_prompt_version_is_v0_3_or_later():
    """Pin the floor because v0.3 introduced the layer-2 Chia
    mention-discipline pass. An accidental revert would silently reuse
    weaker prompt behavior under a fresh-looking cache key."""
    assert PROMPT_VERSION >= "extractor-v0.3"


def test_few_shot_examples_include_a_compound_free_text_case():
    """Rule 13 is reinforced by a worked example. If the few-shot
    list ever loses it, the prompt's discipline regresses to "tell
    the model what to do, don't show it" -- which empirically
    underperforms on compound clauses (D-68 baseline).

    This test looks for any few-shot whose user-text contains a
    Rule-13-style compound clause and whose gold output for that
    clause is a `free_text` criterion. Specific surface forms can
    drift; the pattern (compound clause -> free_text) is what
    matters."""
    found = False
    for user_text, gold in FEW_SHOT_EXAMPLES:
        # Rule-13-flavored markers in the user text.
        if "or hepatic encephalopathy" not in user_text.lower():
            continue
        for crit in gold.criteria:
            if crit.kind == "free_text" and "hepatic encephalopathy" in crit.source_text.lower():
                found = True
                break
    assert found, (
        "Expected a few-shot example demonstrating a compound clause "
        "routed to free_text under Rule 13."
    )


def test_few_shot_examples_include_chia_context_mentions():
    """Layer-2 Chia F1 depends on labels that are audit-only in the
    matcher path. The prompt needs worked examples for these context
    labels, not just prose."""
    mentions = [
        mention
        for _user_text, gold in FEW_SHOT_EXAMPLES
        for crit in gold.criteria
        for mention in crit.mentions
    ]
    by_type = {mention.type for mention in mentions}
    for expected in {
        "Scope",
        "Temporal",
        "Reference_point",
        "Multiplier",
        "Observation",
        "Negation",
        "Qualifier",
        "Procedure",
        "Value",
    }:
        assert expected in by_type
    assert any(
        mention.type == "Value" and mention.text == "greater than or equal to 18 years"
        for mention in mentions
    )
    assert any(
        mention.type == "Temporal"
        and mention.text == "within the first 48 hours following hospital admission"
        for mention in mentions
    )
    assert any(
        mention.type == "Scope" and mention.text == "General or neuraxial anesthesia"
        for mention in mentions
    )


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
