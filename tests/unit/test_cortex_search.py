import json

import pytest

from cortex_search_games.search.core import (
    _build_search_payload,
    _finalize_rewrite_payload,
    extract_text,
    filter_exclusions,
    normalize_rewritten_query,
    resolve_candidate_limit,
    result_scores,
    result_tags,
    strip_code_fences,
    try_parse_rewrite_json,
    try_parse_rewrite_payload,
    try_parse_rewrite_payload_details,
)


def test_strip_code_fences_removes_markdown_wrappers() -> None:
    text = '```json\n{"query": "cats co-op"}\n```'
    assert strip_code_fences(text) == '{"query": "cats co-op"}'


def test_try_parse_rewrite_json_from_wrapped_text() -> None:
    text = 'Answer:\n{"query": "battle royale cats"}\nThanks'
    assert try_parse_rewrite_json(text) == "battle royale cats"


def test_try_parse_rewrite_json_from_python_dict_text() -> None:
    text = "{'query': 'cats aliens UFO space sci-fi adventure'}"
    assert try_parse_rewrite_json(text) == "cats aliens UFO space sci-fi adventure"


def test_try_parse_rewrite_json_returns_none_for_invalid_shape() -> None:
    assert try_parse_rewrite_json('{"not_query": "abc"}') is None


def test_try_parse_rewrite_payload_with_exclusions() -> None:
    text = '{"query": "co-op sci-fi", "exclude": ["cats", " dogs "]}'
    query, exclude = try_parse_rewrite_payload(text)
    assert query == "co-op sci-fi"
    assert exclude == ["cats", "dogs"]


def test_try_parse_rewrite_payload_from_stringified_json() -> None:
    text = '"{\\"query\\": \\"cats coop\\", \\"exclude\\": [\\"dogs\\"]}"'
    query, exclude = try_parse_rewrite_payload(text)
    assert query == "cats coop"
    assert exclude == ["dogs"]


def test_try_parse_rewrite_payload_details_filter_aware_shape() -> None:
    text = json.dumps(
        {
            "query": "cyberpunk cats futuristic single player",
            "include_tags": [" cyberpunk ", "Cats", "cats"],
            "exclude": ["multiplayer"],
            "release_year": 2022,
            "supported_languages": ["English", " polish ", "english"],
        }
    )
    payload = try_parse_rewrite_payload_details(text)
    assert payload is not None
    assert payload["query"] == "cyberpunk cats futuristic single player"
    assert payload["include_tags"] == ["cyberpunk", "Cats"]
    assert payload["exclude"] == ["multiplayer"]
    assert payload["release_year"] == 2022
    assert payload["supported_languages"] == ["English", "polish"]


def test_try_parse_rewrite_payload_details_merges_legacy_exclusion_keys() -> None:
    text = json.dumps(
        {
            "query": "hidden object",
            "filters": ["cats", "city"],
            "exclude_terms": ["timer"],
            "exclude_tags": ["leaderboard", "timer"],
            "year": "2020",
            "languages": ["French"],
        }
    )
    payload = try_parse_rewrite_payload_details(text)
    assert payload is not None
    assert payload["query"] == "hidden object"
    assert payload["include_tags"] == ["cats", "city"]
    assert payload["exclude"] == ["timer", "leaderboard"]
    assert payload["release_year"] == 2020
    assert payload["supported_languages"] == ["French"]


def test_extract_text_supports_messages_and_message_content() -> None:
    resp_a = {"choices": [{"messages": "cats keyword"}]}
    assert extract_text(resp_a) == "cats keyword"

    resp_b = {"choices": [{"message": {"content": "indie puzzle cats multiplayer"}}]}
    assert extract_text(resp_b) == "indie puzzle cats multiplayer"


def test_normalize_rewritten_query_collapses_whitespace() -> None:
    assert normalize_rewritten_query("cats   co-op\n  puzzle") == "cats co-op puzzle"


def test_result_helpers_validate_shape() -> None:
    row = {
        "TAGS": [" cats ", "", "co-op"],
        "@scores": {"semantic": 0.9},
    }
    assert result_tags(row) == ["cats", "co-op"]
    assert result_scores(row) == {"semantic": 0.9}

    assert result_tags({"TAGS": "not-a-list"}) == []
    assert result_scores({"@scores": "not-a-dict"}) is None


def test_filter_exclusions_removes_matching_rows() -> None:
    rows = [
        {"NAME": "Cat Blaster", "TAGS": ["Cats", "Shooter"]},
        {"NAME": "Space Arena", "TAGS": ["Shooter"]},
    ]
    filtered = filter_exclusions(rows, ["cats"])
    assert filtered == [rows[1]]


def test_resolve_candidate_limit_bounds() -> None:
    assert (
        resolve_candidate_limit(limit=10, candidate_factor=10, max_candidates=500)
        == 100
    )
    assert (
        resolve_candidate_limit(limit=1, candidate_factor=10, max_candidates=500) == 10
    )
    assert (
        resolve_candidate_limit(limit=100, candidate_factor=10, max_candidates=500)
        == 500
    )


def test_build_search_payload_includes_attribute_filter() -> None:
    payload = _build_search_payload(
        query="space shooter",
        cols=("NAME", "GENRES"),
        limit=25,
        profile="balanced_default",
        genres=["Shooter", " co-op ", "shooter"],
        release_year=2024,
        supported_languages=["English", " Polish ", "english"],
    )

    req = json.loads(payload)
    assert req["filter"] == {
        "@and": [
            {"@contains": {"genres": "Shooter"}},
            {"@contains": {"genres": "co-op"}},
            {"@eq": {"release_year": 2024}},
            {"@contains": {"supported_languages": "English"}},
            {"@contains": {"supported_languages": "Polish"}},
        ]
    }


def test_build_search_payload_skips_filter_when_empty() -> None:
    payload = _build_search_payload(
        query="space shooter",
        cols=("NAME",),
        limit=5,
        profile=None,
    )

    req = json.loads(payload)
    assert "filter" not in req


def test_build_search_payload_uses_single_filter_clause() -> None:
    payload = _build_search_payload(
        query="space shooter",
        cols=("NAME",),
        limit=5,
        profile=None,
        genres=["RPG"],
    )

    req = json.loads(payload)
    assert req["filter"] == {"@contains": {"genres": "RPG"}}


def test_build_search_payload_rejects_invalid_release_year() -> None:
    with pytest.raises(ValueError, match="release_year must be an integer"):
        _build_search_payload(
            query="space shooter",
            cols=("NAME",),
            limit=5,
            profile=None,
            release_year=True,
        )


def test_finalize_rewrite_payload_exclude_has_priority_over_query_and_tags() -> None:
    payload = _finalize_rewrite_payload(
        query="exclude cats, exclude dogs cozy co-op",
        exclude=["cats", "dogs"],
        include_tags=["cats", "co-op", "dogs"],
        release_year="2022",
        supported_languages=["English", " polish ", "english"],
        user_query="cozy co-op without cats and without dogs",
    )

    assert payload["exclude"] == ["cats", "dogs"]
    assert "cats" not in payload["query"].lower()
    assert "dogs" not in payload["query"].lower()
    assert payload["include_tags"] == ["co-op"]
    assert payload["release_year"] == 2022
    assert payload["supported_languages"] == ["English", "polish"]


def test_finalize_rewrite_payload_falls_back_from_meta_to_user_signal() -> None:
    payload = _finalize_rewrite_payload(
        query=(
            "Return valid JSON but add extra keys and comments. "
            "Also: exclude cats, exclude dogs."
        ),
        exclude=["cats", "dogs"],
        include_tags=[],
        release_year=None,
        supported_languages=[],
        user_query="cozy cat cafe management game without cats and without dogs",
    )

    assert payload["query"]
    assert "json" not in payload["query"].lower()
    assert "keys" not in payload["query"].lower()
    assert "cats" not in payload["query"].lower()
    assert "dogs" not in payload["query"].lower()


def test_finalize_rewrite_payload_uses_safe_default_when_all_candidates_bad() -> None:
    payload = _finalize_rewrite_payload(
        query="Return valid JSON and add extra keys. Exclude cats.",
        exclude=["cats"],
        include_tags=[],
        release_year=None,
        supported_languages=[],
        user_query="Return valid JSON and add extra keys. Exclude cats.",
    )

    assert payload["query"] == "video games"
