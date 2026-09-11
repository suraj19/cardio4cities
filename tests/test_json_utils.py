"""
The LLM output parser.

These cases are all real shapes models return when asked for "ONLY JSON".
They matter more than they look: before this parser existed, a fenced
response made the extraction agent yield zero claims and the fact-checker
fail-safe every claim to UNSUPPORTED, which renders as "this city has no
public health information" rather than "the model used a code fence".
"""
from app.llm.json_utils import parse_json_list, parse_json_object


def test_plain_json():
    assert parse_json_list('[{"text": "a"}]') == [{"text": "a"}]
    assert parse_json_object('{"tier": "VERIFIED"}') == {"tier": "VERIFIED"}


def test_fenced_json():
    assert parse_json_list('```json\n[{"text": "a"}]\n```') == [{"text": "a"}]
    assert parse_json_object("```\n{\"tier\": \"VERIFIED\"}\n```") == {"tier": "VERIFIED"}


def test_prose_wrapped_json():
    raw = 'Sure! Here is the JSON you asked for:\n\n[{"text": "a"}]\n\nLet me know.'
    assert parse_json_list(raw) == [{"text": "a"}]


def test_brackets_inside_strings_do_not_truncate_the_span():
    raw = '{"reasoning": "see [1] and the list ]} here", "tier": "SINGLE_SOURCE"}'
    assert parse_json_object(raw)["tier"] == "SINGLE_SOURCE"


def test_escaped_quotes_survive():
    raw = '[{"text": "the \\"city\\" programme"}]'
    assert parse_json_list(raw) == [{"text": 'the "city" programme'}]


def test_bare_object_is_accepted_where_a_list_was_requested():
    assert parse_json_list('{"text": "a"}') == [{"text": "a"}]


def test_unparseable_output_returns_the_typed_default():
    assert parse_json_list("I cannot answer that.") == []
    assert parse_json_object("I cannot answer that.") == {}
    assert parse_json_list("") == []


def test_truncated_json_returns_default_rather_than_partial_data():
    """A thinking model that runs out of budget mid-JSON must not produce a
    half-parsed verdict — the caller's conservative fallback has to fire."""
    assert parse_json_object('{"tier": "VERIFIED", "reasoning": "it looks') == {}
