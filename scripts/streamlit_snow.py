import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import streamlit as st
from snowflake.core import Root
from snowflake.core.exceptions import APIError
from snowflake.snowpark.context import get_active_session
from snowflake.snowpark.exceptions import (
    SnowparkClientException,
    SnowparkSessionException,
    SnowparkSQLException,
)

# DEFAULTS CONFIGURATION

PAGE_TITLE = "Find your new fave game"
PAGE_ICON = "🎮"
APP_TITLE = "Find your new fave game"
APP_CAPTION = "Semantic search over the Steam games catalog (Snowflake Cortex Search)."
QUERY_PLACEHOLDER = "I want a co-op survival game, but not horror."

DB = "CORTEX_DB"
SCHEMA = "RAW"
SERVICE = "GAMES_SVC_1_5"
SERVICE_FQN = f"{DB}.{SCHEMA}.{SERVICE}"

RETURN_COLS = [
    "NAME",
    "ABOUT_THE_GAME",
    "RELEASE_YEAR",
    "SUPPORTED_LANGUAGES",
    "CATEGORIES",
    "GENRES",
    "TAGS",
]
MIN_SCORE = 0.75
SHOW_ALL_MIN_SCORE = 0.75
MAX_CORTEX_SEARCH_LIMIT = 1000
MAX_DEBUG_SCAN_ROWS = 50
DEFAULT_LLM_MAX_TOKENS = 120

DEFAULT_SYSTEM_PROMPT = """
You rewrite user queries for hybrid (keyword + vector) search
over a video games catalog.

Return ONLY a valid JSON object with exactly five keys:
"query", "include_tags", "exclude", "release_year", "supported_languages".

Hard requirements:
- Output MUST be valid JSON (double quotes, no trailing commas).
- Output MUST be a single JSON object and nothing else.
- Output MUST be a single line.
- Do NOT wrap the JSON in markdown fences/backticks and do NOT add explanations.
- Always include all keys:
  - "query": a string
  - "include_tags": an array of strings (use [] if none)
  - "exclude": an array of strings (use [] if none)
  - "release_year": an integer year or null
  - "supported_languages": an array of strings (use [] if none)
- Do NOT return JSON as a string (no extra quotes around the whole object).
- Do NOT add any additional keys.

Input format:
- The user query will appear as a line starting with: User query:
- Use only that text as the input query to rewrite.

Rewrite rules:
- "query" must be short, English, keyword-rich,
  suitable for hybrid (keyword + vector) search.
- Prefer 5–20 keywords / short phrases, not full sentences (less noise for embeddings).
- Preserve user-provided keywords/tags (do not drop them).
- Preserve concrete mechanic/mode/tag terms literally (helps keyword stage).
- Add synonyms only when needed; avoid over-expansion that makes the query too generic.
- Do NOT invent game titles. Focus on genres, mechanics, themes, and features.
- If the query is already good, return it unchanged (normalize whitespace only).

Attribute extraction:
- Put in "include_tags" only explicit, non-negated user terms
  that look like tags/themes.
- If a term appears in "exclude", it must NOT appear in "include_tags" or "query".
- Extract "release_year" only when explicitly requested
  as a single year (otherwise null).
- Extract "supported_languages" only when explicitly requested.

Exclusions and negations:
- If the user explicitly excludes something via negation (no/without/not/avoid/exclude),
  add that term to "exclude".
- "exclude" items should be simple keywords/tags, lowercase, no punctuation.
- Exclusions have priority over the main query and include_tags.

Examples:
Input: User query: co-op multiplayer games set in Japan without cats
Output: {"query":"co-op multiplayer Japan","include_tags":[],"exclude":["cats"],
"release_year":null,"supported_languages":[]}

Input: User query: battle royale with building, looting resources, and combat
Output: {"query":"battle royale building looting combat","include_tags":[],
"exclude":[],"release_year":null,"supported_languages":[]}

Input: User query: puzzle game not horror, no gore
Output: {"query":"puzzle","include_tags":[],"exclude":["horror","gore"],
"release_year":null,"supported_languages":[]}

Input: User query: i want to play something like cyberpunk but with cats, no multiplayer
Output: {"query":"cyberpunk cats futuristic sci-fi single player",
"include_tags":["cyberpunk","cats"],"exclude":["multiplayer"],
"release_year":null,"supported_languages":[]}

Input: User query: french hidden object game from 2022 without timer
Output: {"query":"hidden object","include_tags":[],"exclude":["timer"],
"release_year":2022,"supported_languages":["french"]}
""".strip()

LLM_MODELS = [
    "claude-4-sonnet",
    "openai-gpt-4.1",
]
SCORING_OPTIONS = [
    "balanced_default",
    "keyword_focus",
    "semantic_focus",
    "keyword_extreme",
    "no_reranker_balanced",
    "low_latency",
    "reranker_heavy",
]
DEFAULT_SCORING = "balanced_default"


# HELPER FUNCTIONS


@st.cache_resource
def _get_local_session():
    src_path = Path(__file__).resolve().parents[1] / "src"
    if str(src_path) not in sys.path:
        sys.path.append(str(src_path))
    from cortex_search_games.utils.snowflake_conn import get_session

    return get_session("cortex")


def _get_session():
    try:
        return get_active_session()
    except (SnowparkClientException, SnowparkSessionException):
        return _get_local_session()


def _escape_sql(val: str) -> str:
    return val.replace("'", "''")


def _strip_code(text: str) -> str:
    txt = (text or "").strip()
    if not txt.startswith("```"):
        return txt
    return "\n".join(
        line for line in txt.splitlines() if not line.strip().startswith("```")
    ).strip()


def _coerce_dict(parsed: Any) -> dict[str, Any] | None:
    for _ in range(2):
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            inner = parsed.strip()
            try:
                parsed = json.loads(inner)
                continue
            except json.JSONDecodeError:
                try:
                    parsed = ast.literal_eval(inner)
                    continue
                except (ValueError, SyntaxError):
                    return None
        else:
            return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_filter_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        txt = str(raw).strip()
        if not txt:
            continue
        key = txt.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(txt)
    return out


def _normalize_filter_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return _normalize_filter_values([str(v) for v in raw])


def _merge_filter_terms(*term_lists: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for terms in term_lists:
        for raw in terms:
            term = str(raw).strip()
            if not term:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
    return out


def _normalize_release_year(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return None
    if year <= 0:
        return None
    return year


def _empty_rewrite_payload(user_query: str) -> dict[str, Any]:
    return {
        "query": user_query,
        "exclude": [],
        "include_tags": [],
        "release_year": None,
        "supported_languages": [],
    }


def _try_parse_payload_details(text: str) -> dict[str, Any] | None:
    txt = _strip_code(text)
    start = txt.find("{")
    end = txt.rfind("}")

    candidates = [txt]
    if start != -1 and end != -1 and end > start:
        snippet = txt[start : end + 1]
        if snippet not in candidates:
            candidates.append(snippet)

    for candidate in candidates:
        parsed: Any
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(candidate)
            except (ValueError, SyntaxError):
                continue

        parsed_dict = _coerce_dict(parsed)
        if parsed_dict is None:
            continue

        query_raw = parsed_dict.get("query")
        if not isinstance(query_raw, str):
            continue

        query = query_raw.strip()
        if not query:
            continue

        include_tags = _normalize_filter_list(
            parsed_dict.get("include_tags", parsed_dict.get("filters", []))
        )
        supported_languages = _normalize_filter_list(
            parsed_dict.get("supported_languages", parsed_dict.get("languages", []))
        )
        release_year = _normalize_release_year(
            parsed_dict.get("release_year", parsed_dict.get("year"))
        )
        exclude = _merge_filter_terms(
            _normalize_filter_list(parsed_dict.get("exclude", [])),
            _normalize_filter_list(parsed_dict.get("exclude_terms", [])),
            _normalize_filter_list(parsed_dict.get("exclude_tags", [])),
        )

        return {
            "query": query,
            "exclude": exclude,
            "include_tags": include_tags,
            "release_year": release_year,
            "supported_languages": supported_languages,
        }

    return None


def _extract_text(resp: dict[str, Any]) -> str | None:
    choices = resp.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        first = choices[0]

        msg = first.get("messages")
        if isinstance(msg, str) and msg.strip():
            return msg

        msg_obj = first.get("message")
        if isinstance(msg_obj, dict):
            content = msg_obj.get("content")
            if isinstance(content, str) and content.strip():
                return content

    return None


def _normalize_query(text: str) -> str:
    return " ".join(_strip_code(text).split()).strip()


_NEGATION_MARKER_RE = re.compile(
    r"\b(?:exclude|excluding|without|avoid|avoiding|not|no)\b",
    flags=re.IGNORECASE,
)
_PUNCT_SEP_RE = re.compile(r"[,;:/|]+")
_PROMPT_META_TOKENS = {
    "add",
    "assistant",
    "comment",
    "comments",
    "exclude",
    "extra",
    "format",
    "ignore",
    "instruction",
    "instructions",
    "json",
    "key",
    "keys",
    "markdown",
    "output",
    "plain",
    "prompt",
    "return",
    "schema",
    "system",
    "text",
    "valid",
}
_PROMPT_META_PHRASES = (
    "valid json",
    "output plain text",
    "ignore system",
    "add extra keys",
    "return only",
    "single json object",
)
_DEFAULT_SAFE_QUERY = "video games"
_LOW_SIGNAL_TOKENS = {
    "a",
    "an",
    "and",
    "also",
    "but",
    "for",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def _compile_term_pattern(term: str) -> re.Pattern[str] | None:
    cleaned = _normalize_query(term)
    if not cleaned:
        return None
    escaped = re.escape(cleaned).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", flags=re.IGNORECASE)


def _text_contains_term(text: str, term: str) -> bool:
    pattern = _compile_term_pattern(term)
    if pattern is None:
        return False
    return pattern.search(text) is not None


def _strip_terms_from_text(
    text: str,
    terms: list[str],
    *,
    drop_negation_markers: bool,
) -> str:
    cleaned = text
    for term in sorted(
        _normalize_filter_values(terms),
        key=lambda item: len(item),
        reverse=True,
    ):
        pattern = _compile_term_pattern(term)
        if pattern is None:
            continue
        cleaned = pattern.sub(" ", cleaned)

    if drop_negation_markers:
        cleaned = _NEGATION_MARKER_RE.sub(" ", cleaned)

    cleaned = _PUNCT_SEP_RE.sub(" ", cleaned)
    return _normalize_query(cleaned)


def _looks_like_prompt_meta_query(text: str) -> bool:
    lowered = _normalize_query(text).lower()
    if not lowered:
        return False

    tokens = re.findall(r"[a-z]{2,}", lowered)
    if not tokens:
        return False

    meta_hits = sum(token in _PROMPT_META_TOKENS for token in tokens)
    if tokens and all(token in _PROMPT_META_TOKENS for token in tokens):
        return True
    if meta_hits >= 3 and meta_hits * 2 >= len(tokens):
        return True

    phrase_hits = sum(phrase in lowered for phrase in _PROMPT_META_PHRASES)
    return phrase_hits > 0 and meta_hits >= 2


def _sanitize_include_tags(
    include_tags: list[str],
    exclude_terms: list[str],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in _normalize_filter_values(include_tags):
        tag = _normalize_query(raw)
        if not tag:
            continue
        if any(_text_contains_term(tag, term) for term in exclude_terms):
            continue
        if _looks_like_prompt_meta_query(tag):
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _strip_prompt_meta_noise(text: str) -> str:
    tokens: list[str] = []
    for raw in text.split():
        token = raw.strip(".,;:!?()[]{}<>\"'`")
        if not token:
            continue
        key = token.lower()
        if key in _PROMPT_META_TOKENS or key in _LOW_SIGNAL_TOKENS:
            continue
        tokens.append(token)
    return _normalize_query(" ".join(tokens))


def _pick_safe_rewritten_query(
    *,
    query: str,
    exclude_terms: list[str],
    include_tags: list[str],
    user_query: str,
) -> str:
    query_clean = _strip_terms_from_text(
        query,
        exclude_terms,
        drop_negation_markers=bool(exclude_terms),
    )
    include_candidate = _strip_terms_from_text(
        " ".join(include_tags),
        exclude_terms,
        drop_negation_markers=bool(exclude_terms),
    )
    user_candidate = _strip_terms_from_text(
        user_query,
        exclude_terms,
        drop_negation_markers=bool(exclude_terms),
    )

    for candidate in (query_clean, include_candidate, user_candidate):
        if candidate and not _looks_like_prompt_meta_query(candidate):
            return candidate

    for candidate in (query_clean, include_candidate, user_candidate):
        denoised = _strip_prompt_meta_noise(candidate)
        if denoised and not _looks_like_prompt_meta_query(denoised):
            return denoised

    return _DEFAULT_SAFE_QUERY


def _finalize_rewrite_payload(
    *,
    query: str,
    exclude: list[str],
    include_tags: list[str],
    release_year: Any,
    supported_languages: list[str],
    user_query: str,
) -> dict[str, Any]:
    exclude_terms = _normalize_filter_values(exclude)
    include_tags_clean = _sanitize_include_tags(include_tags, exclude_terms)
    rewritten = _pick_safe_rewritten_query(
        query=query,
        exclude_terms=exclude_terms,
        include_tags=include_tags_clean,
        user_query=user_query,
    )
    return {
        "query": rewritten,
        "exclude": exclude_terms,
        "include_tags": include_tags_clean,
        "release_year": _normalize_release_year(release_year),
        "supported_languages": _normalize_filter_values(supported_languages),
    }


def _scores(row: dict[str, Any]) -> dict[str, Any] | None:
    scores = row.get("@scores")
    return scores if isinstance(scores, dict) else None


def _format_str_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    if isinstance(val, str):
        txt = val.strip()
        if txt.startswith("[") and txt.endswith("]"):
            try:
                parsed = json.loads(txt)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
    return []


def _format_tags(row: dict[str, Any]) -> list[str]:
    return _format_str_list(row.get("TAGS"))


def _format_langs(row: dict[str, Any]) -> list[str]:
    return _format_str_list(row.get("SUPPORTED_LANGUAGES"))


def _format_categories(row: dict[str, Any]) -> list[str]:
    return _format_str_list(row.get("CATEGORIES"))


def _format_genres(row: dict[str, Any]) -> list[str]:
    return _format_str_list(row.get("GENRES"))


def _row_debug_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "@scores": _scores(row),
        "NAME": row.get("NAME"),
        "ABOUT_THE_GAME": row.get("ABOUT_THE_GAME"),
        "RELEASE_YEAR": row.get("RELEASE_YEAR"),
        "SUPPORTED_LANGUAGES": _format_langs(row),
        "CATEGORIES": _format_categories(row),
        "GENRES": _format_genres(row),
        "TAGS": _format_tags(row),
    }


def _normalize_exclusions(exclude: list[str]) -> list[str]:
    return [term.lower().strip() for term in exclude if term.strip()]


def _match_known_filter_values(
    values: list[str],
    options: list[str],
) -> tuple[list[str], list[str]]:
    if not values:
        return [], []

    option_map = {
        str(opt).strip().lower(): str(opt) for opt in options if str(opt).strip()
    }
    matched: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in values:
        txt = str(raw).strip()
        if not txt:
            continue
        key = txt.lower()
        if key in seen:
            continue
        seen.add(key)
        canonical = option_map.get(key)
        if canonical is None:
            missing.append(txt)
            continue
        matched.append(canonical)

    return matched, missing


def _row_text_blob(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("NAME") or ""),
        str(row.get("ABOUT_THE_GAME") or ""),
    ]

    for vals in (
        _format_tags(row),
        _format_genres(row),
        _format_categories(row),
        _format_langs(row),
    ):
        if vals:
            parts.append(" ".join(vals))

    return " ".join(parts).lower()


def _filter_exclusions(
    rows: list[dict[str, Any]], exclude: list[str]
) -> list[dict[str, Any]]:
    if not exclude:
        return rows
    terms = _normalize_exclusions(exclude)
    if not terms:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        blob = _row_text_blob(row)
        if any(term in blob for term in terms):
            continue
        out.append(row)
    return out


def _score_value(raw: Any) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _normalize_cosine(score: float) -> float:
    # cosine_similarity is [-1, 1], but i map it to [0, 1] for ui thresholds
    return max(0.0, min(1.0, (score + 1.0) / 2.0))


def _score_source(row: dict[str, Any]) -> str | None:
    if _score_value(row.get("@search_score")) is not None:
        return "search_score"

    scores = row.get("@scores")
    if isinstance(scores, dict):
        for key in ("search_score", "score", "final_score", "overall"):
            if _score_value(scores.get(key)) is not None:
                return key
        if _score_value(scores.get("cosine_similarity")) is not None:
            return "cosine_similarity"

    return None


def _row_score(row: dict[str, Any]) -> float | None:
    score = _score_value(row.get("@search_score"))
    if score is not None:
        return score

    scores = row.get("@scores")
    if isinstance(scores, dict):
        for key in ("search_score", "score", "final_score", "overall"):
            score = _score_value(scores.get(key))
            if score is not None:
                return score
        cosine = _score_value(scores.get("cosine_similarity"))
        if cosine is not None:
            return _normalize_cosine(cosine)

    return None


def ensure_service_exists() -> None:
    sess = _get_session()
    try:
        sess.sql(f"DESC CORTEX SEARCH SERVICE {SERVICE_FQN}").collect()
    except SnowparkSQLException as err:
        st.error(f"Can't describe Cortex Search Service: `{SERVICE_FQN}`.")
        st.exception(err)
        st.stop()


def _row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row

    for attr in ("asDict", "as_dict"):
        fn = getattr(row, attr, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                try:
                    return fn(recursive=True)
                except TypeError:
                    return fn()

    if hasattr(row, "keys") and callable(row.keys):
        try:
            keys = list(row.keys())
            return {str(k): row[k] for k in keys}
        except (AttributeError, IndexError, KeyError, TypeError):
            pass

    return {"_row": str(row)}


def _source_table_for_service(service_name: str) -> str:
    service_upper = service_name.upper()
    if "CAT_GAMES" in service_upper:
        return "DT_CAT_GAMES"
    return "DT_GAMES"


@st.cache_data(ttl=900, show_spinner=False)
def load_attribute_filter_options(service_name: str) -> dict[str, Any]:
    sess = _get_session()
    source_table = _source_table_for_service(service_name)
    table_fqn = f"{DB}.{SCHEMA}.{source_table}"

    tags_sql = f"""
    SELECT DISTINCT TRIM(value::string) AS VAL
    FROM {table_fqn},
         LATERAL FLATTEN(input => TAGS)
    WHERE value IS NOT NULL
      AND TRIM(value::string) <> ''
    ORDER BY VAL
    """

    langs_sql = f"""
    SELECT DISTINCT TRIM(value::string) AS VAL
    FROM {table_fqn},
         LATERAL FLATTEN(input => SUPPORTED_LANGUAGES)
    WHERE value IS NOT NULL
      AND TRIM(value::string) <> ''
    ORDER BY VAL
    """

    years_sql = f"""
    SELECT DISTINCT RELEASE_YEAR AS VAL
    FROM {table_fqn}
    WHERE RELEASE_YEAR IS NOT NULL
    ORDER BY VAL DESC
    """

    try:
        tag_rows = sess.sql(tags_sql).collect()
        lang_rows = sess.sql(langs_sql).collect()
        year_rows = sess.sql(years_sql).collect()
    except SnowparkSQLException as err:
        return {
            "tags": [],
            "supported_languages": [],
            "release_years": [],
            "error": str(err),
        }

    years: list[int] = []
    for row in year_rows:
        raw = row[0]
        if raw is None or isinstance(raw, bool):
            continue
        try:
            year = int(raw)
        except (TypeError, ValueError):
            continue
        if year > 0:
            years.append(year)

    return {
        "tags": [str(row[0]) for row in tag_rows if row[0] is not None],
        "supported_languages": [str(row[0]) for row in lang_rows if row[0] is not None],
        "release_years": sorted(set(years), reverse=True),
        "error": None,
    }


@st.cache_data(ttl=900, show_spinner=False)
def load_service_row_count(service_name: str) -> dict[str, Any]:
    sess = _get_session()
    source_table = _source_table_for_service(service_name)
    table_fqn = f"{DB}.{SCHEMA}.{source_table}"

    try:
        rows = sess.sql(f"SELECT COUNT(*) FROM {table_fqn}").collect()
    except SnowparkSQLException as err:
        return {"count": None, "error": str(err)}

    if not rows or rows[0][0] is None:
        return {"count": None, "error": "COUNT(*) returned no value"}

    try:
        count = int(rows[0][0])
    except (TypeError, ValueError):
        return {"count": None, "error": "COUNT(*) is not an integer"}

    return {"count": max(0, count), "error": None}


def _build_attribute_filter(
    *,
    tags: list[str] | None,
    supported_languages: list[str] | None,
    release_year: int | None,
) -> dict[str, Any] | None:
    clauses: list[dict[str, Any]] = []

    for tag in _normalize_filter_values(tags):
        clauses.append({"@contains": {"tags": tag}})

    for language in _normalize_filter_values(supported_languages):
        clauses.append({"@contains": {"supported_languages": language}})

    if release_year is not None:
        if isinstance(release_year, bool):
            raise ValueError("release_year must be an integer")
        try:
            year = int(release_year)
        except (TypeError, ValueError) as err:
            raise ValueError("release_year must be an integer") from err
        if year <= 0:
            raise ValueError("release_year must be > 0")
        clauses.append({"@eq": {"release_year": year}})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"@and": clauses}


def describe_service() -> list[dict[str, Any]]:
    sess = _get_session()
    try:
        rows = sess.sql(f"DESC CORTEX SEARCH SERVICE {SERVICE_FQN}").collect()
    except SnowparkSQLException as err:
        return [{"error": str(err)}]

    return [_row_to_dict(row) for row in rows]


def cortex_search_data_scan(limit: int) -> list[dict[str, Any]]:
    sess = _get_session()
    n = max(1, min(int(limit), MAX_DEBUG_SCAN_ROWS))
    svc = _escape_sql(SERVICE_FQN)

    sql = f"""
    SELECT *
    FROM TABLE(
      CORTEX_SEARCH_DATA_SCAN(
        SERVICE_NAME => '{svc}'
      )
    )
    LIMIT {n}
    """

    try:
        rows = sess.sql(sql).collect()
    except SnowparkSQLException as err:
        st.warning(f"CORTEX_SEARCH_DATA_SCAN failed for `{SERVICE_FQN}`: {err}")
        return []

    return [_row_to_dict(row) for row in rows]


# MAIN LOGIC


def _build_search_request(
    *,
    query: str,
    columns: list[str],
    cand_limit: int | None,
    scoring: str | None,
    tags: list[str] | None = None,
    supported_languages: list[str] | None = None,
    release_year: int | None = None,
) -> dict[str, Any]:
    req: dict[str, Any] = {
        "query": query,
        "columns": columns,
    }
    if cand_limit is not None:
        req["limit"] = cand_limit

    attr_filter = _build_attribute_filter(
        tags=tags,
        supported_languages=supported_languages,
        release_year=release_year,
    )
    if attr_filter:
        req["filter"] = attr_filter

    if scoring:
        req["scoring_profile"] = scoring
    return req


def search_cortex_rest_api(req: dict[str, Any]) -> dict[str, Any]:
    sess = _get_session()
    root = Root(sess)
    svc = root.databases[DB].schemas[SCHEMA].cortex_search_services[SERVICE]

    search_args = {
        "query": req["query"],
        "columns": req.get("columns"),
    }
    if "filter" in req:
        search_args["filter"] = req.get("filter")
    if "limit" in req:
        search_args["limit"] = req.get("limit")
    if "scoring_profile" in req:
        search_args["scoring_profile"] = req["scoring_profile"]

    resp = svc.search(**search_args)
    return {"results": resp.results, "request_id": resp.request_id}


def extract_results(resp: dict[str, Any]) -> list[dict[str, Any]]:
    rows = resp.get("results")
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except json.JSONDecodeError:
            rows = None

    if not isinstance(rows, list):
        return []

    return [row for row in rows if isinstance(row, dict)]


def filter_min_score(
    rows: list[dict[str, Any]], min_score: float | None
) -> list[dict[str, Any]]:
    if min_score is None or min_score <= 0:
        return rows

    scores = [_row_score(row) for row in rows]
    if not any(score is not None for score in scores):
        return rows

    return [
        row
        for row, score in zip(rows, scores, strict=False)
        if score is not None and score >= min_score
    ]


def rewrite_query(
    user_query: str,
    model: str,
    temp: float,
    max_tokens: int,
) -> tuple[dict[str, Any], str | None]:
    sess = _get_session()
    user_prompt = f"User query: {user_query}"
    prompt = f"{DEFAULT_SYSTEM_PROMPT}\n\n{user_prompt}"

    opts = {"temperature": float(temp), "max_tokens": int(max_tokens)}
    opts_json = json.dumps(opts, ensure_ascii=False)

    sql = """
    SELECT AI_COMPLETE(
        ?,
        ?,
        PARSE_JSON(?)
    ) AS RESPONSE
    """

    try:
        try:
            resp = sess.sql(sql, params=[model, prompt, opts_json]).collect()[0][0]
        except TypeError:
            prompt_sql = _escape_sql(prompt)
            opts_sql = _escape_sql(opts_json)
            sql_fb = f"""
            SELECT AI_COMPLETE(
                '{model}',
                '{prompt_sql}',
                PARSE_JSON('{opts_sql}')
            ) AS RESPONSE
            """
            resp = sess.sql(sql_fb).collect()[0][0]
    except SnowparkSQLException as err:
        return _empty_rewrite_payload(user_query), f"LLM SQL error: {err}"

    if resp is None:
        return _empty_rewrite_payload(
            user_query
        ), "LLM error (AI_COMPLETE returned NULL)."

    text = None
    if isinstance(resp, dict):
        text = _extract_text(resp) or json.dumps(resp, ensure_ascii=False)
    elif isinstance(resp, str):
        text = resp
        if resp.lstrip().startswith("{"):
            try:
                maybe = json.loads(resp)
            except json.JSONDecodeError:
                maybe = None
            if isinstance(maybe, dict) and maybe.get("choices") is not None:
                extracted = _extract_text(maybe)
                if extracted:
                    text = extracted
    else:
        text = str(resp)

    if not text.strip():
        return _empty_rewrite_payload(user_query), "LLM returned empty content."

    parsed = _try_parse_payload_details(text)
    if parsed is not None:
        payload = _finalize_rewrite_payload(
            query=str(parsed.get("query") or ""),
            exclude=list(parsed.get("exclude") or []),
            include_tags=list(parsed.get("include_tags") or []),
            release_year=parsed.get("release_year"),
            supported_languages=list(parsed.get("supported_languages") or []),
            user_query=user_query,
        )
        rewritten = str(payload["query"])
    else:
        rewritten_raw = _normalize_query(text)
        looks_like_obj = (
            "query" in rewritten_raw.lower()
            and "{" in rewritten_raw
            and "}" in rewritten_raw
        )
        payload = _finalize_rewrite_payload(
            query=rewritten_raw,
            exclude=[],
            include_tags=[],
            release_year=None,
            supported_languages=[],
            user_query=user_query,
        )
        rewritten = str(payload["query"])
        if looks_like_obj:
            return _empty_rewrite_payload(
                user_query
            ), "LLM returned an invalid query object."

    if not rewritten:
        return _empty_rewrite_payload(user_query), "LLM returned no usable text."

    return payload, None


def render_form(filter_options: dict[str, Any]) -> dict[str, Any]:
    with st.form("search_form"):
        query = st.text_input(
            "Describe the game you want to find",
            placeholder=QUERY_PLACEHOLDER,
            key="query",
        )

        if filter_options.get("error"):
            st.warning(
                "Couldn't load attribute values from the dataset. "
                "Attribute filters are temporarily unavailable."
            )
        show_all = st.checkbox(
            "SHOW ALL results",
            value=False,
            key="show_all",
            help=(
                "Forces full-catalog candidate scan and deterministic ranking options "
                "(cand_limit=FULL, min_score=0.75, no_reranker_balanced, LLM temp=0.0)."
            ),
        )
        limit = int(
            st.number_input(
                "Results to show",
                min_value=1,
                value=10,
                step=1,
                key="limit",
                disabled=show_all,
                help="Ignored when SHOW ALL is enabled.",
            )
        )

        filter_tags = st.multiselect(
            "Tags",
            options=filter_options.get("tags", []),
            default=[],
            placeholder="Type to search tags...",
            key="filter_tags",
        )
        filter_supported_languages = st.multiselect(
            "Languages",
            options=filter_options.get("supported_languages", []),
            default=[],
            placeholder="Type to search languages...",
            key="filter_supported_languages",
        )
        release_year_options = [None, *filter_options.get("release_years", [])]
        filter_release_year = st.selectbox(
            "Release year",
            options=release_year_options,
            index=0,
            key="filter_release_year",
            format_func=lambda val: "Any" if val is None else str(val),
        )

        with st.expander("LLM rewrite", expanded=False):
            use_llm = st.checkbox("Rewrite query with LLM", value=True, key="use_llm")
            llm_model = st.selectbox(
                "LLM model", options=LLM_MODELS, index=0, key="llm_model"
            )
            llm_max_tokens = int(
                st.number_input(
                    "LLM max tokens",
                    min_value=64,
                    max_value=256,
                    value=DEFAULT_LLM_MAX_TOKENS,
                    step=8,
                    key="llm_max_tokens",
                    help=(
                        "120 recommended. Lower = shorter rewrites; "
                        "higher = richer rewrites but slower/less focused."
                    ),
                )
            )
            llm_temp = st.slider(
                "LLM temperature",
                min_value=0.0,
                max_value=0.6,
                value=0.0,
                step=0.05,
                key="llm_temperature",
                disabled=show_all,
                help="Lower = predictable, higher = diverse.",
            )
        with st.expander("Ranking tuning", expanded=False):
            cand_limit = st.selectbox(
                "Candidate results to retrieve (for scoring/filtering)",
                options=["AUTO", "FULL", 50, 100, 250, 500, 1000],
                index=0,
                key="cand_limit",
                disabled=show_all,
                help=(
                    "Higher = better results but slower. "
                    "AUTO = 10x the display limit, "
                    "FULL = source table row count (capped by API limit)."
                ),
            )
            scoring = st.selectbox(
                "Scoring profile",
                options=SCORING_OPTIONS,
                index=SCORING_OPTIONS.index(DEFAULT_SCORING),
                key="scoring_profile",
                disabled=show_all,
                format_func=lambda v: v if v else "Default service ranking",
            )
            min_score = st.slider(
                "Minimum score threshold",
                min_value=0.0,
                max_value=1.0,
                value=MIN_SCORE,
                step=0.05,
                key="min_score",
                disabled=show_all,
                help=(
                    "0 = no filtering, 0.3 = minimum, "
                    "0.6-0.75 recommended, >0.75 = very strict. "
                    "SHOW ALL enforces 0.75."
                ),
            )
        with st.expander("Debugging", expanded=False):
            debug_include_search_text = st.checkbox(
                "Include SEARCH_TEXT in results (debug)",
                value=True,
                key="debug_include_search_text",
            )
            debug_show_request = st.checkbox(
                "Show request JSON (CORTEX SEARCH API)",
                value=True,
                key="debug_show_request",
            )
            debug_show_response = st.checkbox(
                "Show raw response (CORTEX SEARCH API)",
                value=True,
                key="debug_show_response",
            )
            debug_show_desc = st.checkbox(
                "Show service describe (DESC CORTEX SEARCH SERVICE)",
                value=True,
                key="debug_show_desc",
            )
            debug_show_scan = st.checkbox(
                "Show index data scan (CORTEX_SEARCH_DATA_SCAN)",
                value=True,
                key="debug_show_scan",
            )
            debug_scan_limit = int(
                st.number_input(
                    "Data scan rows",
                    min_value=1,
                    max_value=MAX_DEBUG_SCAN_ROWS,
                    value=5,
                    step=1,
                    key="debug_scan_limit",
                )
            )
            debug_show_vectors = st.checkbox(
                "Show embedding vectors in scan (heavy)",
                value=False,
                key="debug_show_vectors",
            )

        submitted = st.form_submit_button(
            "Search", type="primary", use_container_width=True
        )

    return {
        "query": query,
        "show_all": show_all,
        "limit": limit,
        "cand_limit": cand_limit,
        "use_llm": use_llm,
        "llm_model": llm_model,
        "llm_temp": llm_temp,
        "llm_max_tokens": llm_max_tokens,
        "filter_tags": filter_tags,
        "filter_supported_languages": filter_supported_languages,
        "filter_release_year": filter_release_year,
        "scoring": scoring,
        "min_score": min_score,
        "debug_include_search_text": debug_include_search_text,
        "debug_show_request": debug_show_request,
        "debug_show_response": debug_show_response,
        "debug_show_desc": debug_show_desc,
        "debug_show_scan": debug_show_scan,
        "debug_scan_limit": debug_scan_limit,
        "debug_show_vectors": debug_show_vectors,
        "submitted": submitted,
    }


def show_results(
    rows: list[dict[str, Any]],
    limit: int,
) -> None:
    st.subheader(f"Results shown ({len(rows)})")

    if not rows:
        st.info(
            "No results for the current query. Try another query or lower min score"
        )
        return

    scores = [_row_score(row) for row in rows]
    if rows and all(score is None for score in scores):
        st.info("Scores are missing in results. Min score filtering is disabled")

    for idx, row in enumerate(rows[:limit], start=1):
        name = row.get("NAME")
        about = row.get("ABOUT_THE_GAME") or ""
        score = _row_score(row)

        with st.container():
            st.markdown(f"### {idx}. {name}")

            if about:
                excerpt = about if len(about) <= 400 else f"{about[:400].rstrip()}..."
                st.write(excerpt)

            with st.expander("Ranking components (@scores)", expanded=False):
                if score is not None:
                    st.write(f"Score: {score:.3f}")
                else:
                    st.write("Score: n/a")
                score_src = _score_source(row)
                if score_src:
                    st.caption(f"Score source: {score_src}")
                search_text = row.get("SEARCH_TEXT") or ""
                if search_text:
                    st.caption("SEARCH_TEXT (indexed)")
                    st.code(search_text)
                st.json(_row_debug_payload(row))

        st.divider()


def main() -> None:
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=PAGE_ICON,
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title(APP_TITLE)
    st.caption(APP_CAPTION)

    ensure_service_exists()
    filter_options = load_attribute_filter_options(SERVICE)
    form = render_form(filter_options)

    if not form["submitted"]:
        if not form["query"]:
            st.info("Enter a query and click **Search** to find games!")
        return

    q = (form["query"] or "").strip()
    if not q:
        st.warning("Enter a query")
        return

    exclude_terms: list[str] = []
    llm_tags_raw: list[str] = []
    llm_languages_raw: list[str] = []
    llm_release_year_raw: int | None = None
    llm_tags_matched: list[str] = []
    llm_languages_matched: list[str] = []
    llm_tags_unknown: list[str] = []
    llm_languages_unknown: list[str] = []
    llm_release_year_effective: int | None = None
    show_all = bool(form["show_all"])
    effective_scoring = (
        "no_reranker_balanced" if show_all else (form["scoring"] or None)
    )
    effective_min_score = SHOW_ALL_MIN_SCORE if show_all else float(form["min_score"])
    effective_llm_temp = 0.0 if show_all else float(form["llm_temp"])
    cand_limit_mode = "FULL" if show_all else form["cand_limit"]
    if show_all:
        st.caption(
            "SHOW ALL enabled: cand_limit=FULL, "
            f"min_score={SHOW_ALL_MIN_SCORE:.1f}, "
            "scoring=no_reranker_balanced, llm_temp=0.0."
        )

    if cand_limit_mode == "AUTO":
        cand_limit: int | None = form["limit"] * 10
    elif cand_limit_mode == "FULL":
        full_info = load_service_row_count(SERVICE)
        full_count = full_info["count"]
        if full_info["error"] or full_count is None:
            st.warning(
                "Couldn't resolve FULL candidate limit from source table. "
                "Using AUTO (10x display limit)."
            )
            cand_limit = form["limit"] * 10
        elif full_count <= 0:
            st.warning(
                "Source table has no rows. "
                "Using AUTO candidate limit (10x display limit)."
            )
            cand_limit = form["limit"] * 10
        else:
            capped_limit = min(int(full_count), MAX_CORTEX_SEARCH_LIMIT)
            cand_limit = capped_limit
            if int(full_count) > MAX_CORTEX_SEARCH_LIMIT:
                st.caption(
                    "FULL candidate limit resolved to "
                    f"{full_count} rows from `{_source_table_for_service(SERVICE)}` "
                    f"and capped to {MAX_CORTEX_SEARCH_LIMIT} by API limit."
                )
            else:
                st.caption(
                    "FULL candidate limit resolved to "
                    f"{cand_limit} rows from `{_source_table_for_service(SERVICE)}`."
                )
    else:
        cand_limit = int(cand_limit_mode)

    if form["use_llm"]:
        with st.spinner("Rewriting your query for better results..."):
            rewrite_payload, err = rewrite_query(
                q,
                form["llm_model"],
                effective_llm_temp,
                form["llm_max_tokens"],
            )
        if err:
            st.warning(f"LLM: {err} Using the original query")
        else:
            q = str(rewrite_payload.get("query") or q)
            exclude_terms = _normalize_exclusions(
                list(rewrite_payload.get("exclude") or [])
            )
            llm_tags_raw = _normalize_filter_values(
                list(rewrite_payload.get("include_tags") or [])
            )
            llm_languages_raw = _normalize_filter_values(
                list(rewrite_payload.get("supported_languages") or [])
            )
            llm_release_year_raw = _normalize_release_year(
                rewrite_payload.get("release_year")
            )

            llm_tags_matched, llm_tags_unknown = _match_known_filter_values(
                llm_tags_raw,
                list(filter_options.get("tags", [])),
            )
            llm_languages_matched, llm_languages_unknown = _match_known_filter_values(
                llm_languages_raw,
                list(filter_options.get("supported_languages", [])),
            )
            release_years: set[int] = set()
            for raw in filter_options.get("release_years", []):
                year = _normalize_release_year(raw)
                if year is not None:
                    release_years.add(year)
            if llm_release_year_raw is None:
                llm_release_year_effective = None
            elif not release_years or llm_release_year_raw in release_years:
                llm_release_year_effective = llm_release_year_raw
            else:
                llm_release_year_effective = None

            with st.expander("LLM rewritten query", expanded=False):
                st.code(q)
                if exclude_terms:
                    st.caption(f"Excluded terms: {', '.join(exclude_terms)}")
                if llm_tags_matched:
                    st.caption(f"LLM tag filters: {', '.join(llm_tags_matched)}")
                if llm_languages_matched:
                    st.caption(
                        f"LLM language filters: {', '.join(llm_languages_matched)}"
                    )
                if llm_release_year_effective is not None:
                    st.caption(f"LLM release year filter: {llm_release_year_effective}")
                if llm_tags_unknown:
                    st.caption(
                        "Ignored LLM tags not in attribute dictionary: "
                        f"{', '.join(llm_tags_unknown)}"
                    )
                if llm_languages_unknown:
                    st.caption(
                        "Ignored LLM languages not in attribute dictionary: "
                        f"{', '.join(llm_languages_unknown)}"
                    )
                if (
                    llm_release_year_raw is not None
                    and llm_release_year_effective is None
                ):
                    st.caption("Ignored LLM release year not found in current dataset.")

    selected_tags = _normalize_filter_values(
        _merge_filter_terms(list(form["filter_tags"]), llm_tags_matched)
    )
    selected_languages = _normalize_filter_values(
        _merge_filter_terms(
            list(form["filter_supported_languages"]),
            llm_languages_matched,
        )
    )
    manual_release_year = _normalize_release_year(form.get("filter_release_year"))
    selected_release_year = (
        manual_release_year
        if manual_release_year is not None
        else llm_release_year_effective
    )

    cols = list(RETURN_COLS)
    if form["debug_include_search_text"] and "SEARCH_TEXT" not in cols:
        cols.append("SEARCH_TEXT")

    req = _build_search_request(
        query=q,
        columns=cols,
        cand_limit=cand_limit,
        scoring=effective_scoring,
        tags=selected_tags,
        supported_languages=selected_languages,
        release_year=selected_release_year,
    )

    try:
        resp = search_cortex_rest_api(req)
    except (APIError, SnowparkSQLException, TypeError, ValueError) as err:
        st.error("Cortex Search API request failed.")
        st.exception(err)
        return

    rows = filter_min_score(extract_results(resp), effective_min_score)
    rows = _filter_exclusions(rows, exclude_terms)

    if any(
        (
            form["debug_show_request"],
            form["debug_show_response"],
            form["debug_show_desc"],
            form["debug_show_scan"],
        )
    ):
        with st.expander("Debug: Service & Index", expanded=False):
            request_id = resp.get("request_id")
            if request_id:
                st.caption(f"request_id: {request_id}")

            if form["debug_show_request"]:
                st.subheader("CORTEX SEARCH API request")
                st.json(req)

            if form["debug_show_response"]:
                st.subheader("CORTEX SEARCH API response (metadata)")
                meta = {k: v for k, v in resp.items() if k != "results"}
                meta["results_count"] = len(extract_results(resp))
                st.json(meta)

            if form["debug_show_desc"]:
                st.subheader("DESC CORTEX SEARCH SERVICE")
                st.dataframe(describe_service())

            if form["debug_show_scan"]:
                st.subheader("CORTEX_SEARCH_DATA_SCAN (sample)")
                scan_rows = cortex_search_data_scan(form["debug_scan_limit"])
                if scan_rows:
                    embed_cols = [
                        str(k)
                        for k in scan_rows[0]
                        if str(k).startswith("_GENERATED_EMBEDDINGS_")
                    ]
                    if embed_cols:
                        st.caption(f"Embedding columns: {', '.join(embed_cols)}")

                    if form["debug_show_vectors"]:
                        st.dataframe(scan_rows)
                    else:
                        preview: list[dict[str, Any]] = []
                        for row in scan_rows:
                            item: dict[str, Any] = {
                                "NAME": row.get("NAME"),
                                "SEARCH_TEXT": (row.get("SEARCH_TEXT") or "")[:200],
                            }
                            for col in embed_cols:
                                vec = row.get(col)
                                dim = None
                                if vec is not None and not isinstance(vec, str):
                                    try:
                                        dim = len(vec)
                                    except TypeError:
                                        dim = None
                                item[f"{col}__dim"] = dim
                            preview.append(item)
                        st.dataframe(preview)

    display_limit = len(rows) if show_all else int(form["limit"])
    show_results(rows, display_limit)


if __name__ == "__main__":
    main()
