import ast
import json
import re
from collections.abc import Sequence
from typing import Any

from snowflake.core import Root
from snowflake.core.exceptions import APIError
from snowflake.snowpark import Session
from snowflake.snowpark.exceptions import SnowparkSQLException


def _normalize_filter_values(values: Sequence[str] | None) -> list[str]:
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


def _merge_filter_terms(*term_lists: Sequence[str]) -> list[str]:
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


def strip_code_fences(text: str) -> str:
    txt = text.strip()
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


def try_parse_rewrite_payload_details(text: str) -> dict[str, Any] | None:
    txt = strip_code_fences(text)

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


def try_parse_rewrite_payload(text: str) -> tuple[str | None, list[str]]:
    parsed = try_parse_rewrite_payload_details(text)
    if parsed is None:
        return None, []
    query = parsed.get("query")
    if not isinstance(query, str) or not query.strip():
        return None, []
    return query, list(parsed.get("exclude") or [])


def try_parse_rewrite_json(text: str) -> str | None:
    query, _ = try_parse_rewrite_payload(text)
    return query


def extract_text(resp: dict[str, Any]) -> str | None:
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first = choices[0]
    if not isinstance(first, dict):
        return None

    msgs = first.get("messages")
    if isinstance(msgs, str) and msgs.strip():
        return msgs

    msg = first.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content

    return None


def normalize_rewritten_query(text: str) -> str:
    return " ".join(strip_code_fences(text).split()).strip()


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
    cleaned = normalize_rewritten_query(term)
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
    terms: Sequence[str],
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
    return normalize_rewritten_query(cleaned)


def _looks_like_prompt_meta_query(text: str) -> bool:
    lowered = normalize_rewritten_query(text).lower()
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
    include_tags: Sequence[str],
    exclude_terms: Sequence[str],
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in _normalize_filter_values(include_tags):
        tag = normalize_rewritten_query(raw)
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
    return normalize_rewritten_query(" ".join(tokens))


def _pick_safe_rewritten_query(
    *,
    query: str,
    exclude_terms: Sequence[str],
    include_tags: Sequence[str],
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
    exclude: Sequence[str],
    include_tags: Sequence[str],
    release_year: Any,
    supported_languages: Sequence[str],
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


def result_tags(row: dict[str, Any]) -> list[str]:
    return _format_tags(row)


def result_scores(row: dict[str, Any]) -> dict[str, Any] | None:
    scores = row.get("@scores")
    return scores if isinstance(scores, dict) else None


def _normalize_exclusions(exclude: list[str]) -> list[str]:
    return [term.lower().strip() for term in exclude if term.strip()]


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


def filter_exclusions(
    rows: list[dict[str, Any]],
    exclude: list[str],
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
    return max(0.0, min(1.0, (score + 1.0) / 2.0))


def row_score(row: dict[str, Any]) -> float | None:
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


def resolve_candidate_limit(
    *,
    limit: int,
    candidate_factor: int,
    max_candidates: int,
    min_candidates: int = 1,
) -> int:
    return min(max_candidates, max(min_candidates, limit * candidate_factor))


def _build_attribute_filter(
    *,
    tags: Sequence[str] | None = None,
    genres: Sequence[str] | None = None,
    release_year: int | None = None,
    supported_languages: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    clauses: list[dict[str, Any]] = []

    for tag in _normalize_filter_values(tags):
        clauses.append({"@contains": {"tags": tag}})

    for genre in _normalize_filter_values(genres):
        clauses.append({"@contains": {"genres": genre}})

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

    for language in _normalize_filter_values(supported_languages):
        clauses.append({"@contains": {"supported_languages": language}})

    if not clauses:
        return None

    if len(clauses) == 1:
        return clauses[0]

    return {"@and": clauses}


def _build_search_args(
    *,
    query: str,
    columns: Sequence[str],
    limit: int | None,
    scoring_profile: str | None,
    tags: Sequence[str] | None = None,
    genres: Sequence[str] | None = None,
    release_year: int | None = None,
    supported_languages: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not query.strip():
        raise ValueError("query cannot be empty")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0")

    cols_list = [col.strip() for col in columns if isinstance(col, str) and col.strip()]
    if not cols_list:
        raise ValueError("columns cannot be empty")

    search_args: dict[str, Any] = {
        "query": query,
        "columns": cols_list,
    }
    if limit is not None:
        search_args["limit"] = int(limit)

    attr_filter = _build_attribute_filter(
        tags=tags,
        genres=genres,
        release_year=release_year,
        supported_languages=supported_languages,
    )
    if attr_filter:
        search_args["filter"] = attr_filter

    if scoring_profile:
        search_args["scoring_profile"] = scoring_profile

    return search_args


def _build_search_payload(
    *,
    query: str,
    cols: Sequence[str],
    limit: int,
    profile: str | None,
    tags: Sequence[str] | None = None,
    genres: Sequence[str] | None = None,
    release_year: int | None = None,
    supported_languages: Sequence[str] | None = None,
) -> str:
    search_args = _build_search_args(
        query=query,
        columns=cols,
        limit=limit,
        scoring_profile=profile,
        tags=tags,
        genres=genres,
        release_year=release_year,
        supported_languages=supported_languages,
    )
    return json.dumps(search_args, ensure_ascii=False)


def _fetch_single_value(
    session: Session,
    sql: str,
    params: Sequence[Any] = (),
) -> Any:
    try:
        rows = session.sql(sql, params=params).collect()
    except SnowparkSQLException as err:
        raise RuntimeError(f"Snowflake query failed: {err}") from err

    if not rows:
        raise RuntimeError("Snowflake query returned no rows.")

    return rows[0][0]


def ensure_service_exists(session: Session, svc: str) -> None:
    try:
        session.sql(f"DESC CORTEX SEARCH SERVICE {svc}").collect()
    except SnowparkSQLException as err:
        raise RuntimeError(f"Can't describe Cortex Search Service: {svc}.") from err


def rewrite_query_with_llm(
    session: Session,
    *,
    user_query: str,
    model: str,
    temperature: float,
    max_tokens: int = 120,
    system_prompt: str,
) -> tuple[str, list[str], str | None]:
    parsed, err = rewrite_query_with_llm_details(
        session,
        user_query=user_query,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        system_prompt=system_prompt,
    )
    if parsed is None:
        return user_query, [], err
    return str(parsed["query"]), list(parsed["exclude"]), err


def rewrite_query_with_llm_details(
    session: Session,
    *,
    user_query: str,
    model: str,
    temperature: float,
    max_tokens: int = 120,
    system_prompt: str,
) -> tuple[dict[str, Any] | None, str | None]:
    user_prompt = f"User query: {user_query}"
    prompt = f"{system_prompt}\n\n{user_prompt}"
    opts = {"temperature": float(temperature), "max_tokens": int(max_tokens)}

    opts_json = json.dumps(opts, ensure_ascii=False)

    sql = """
    SELECT AI_COMPLETE(
        ?,
        ?,
        PARSE_JSON(?)
    ) AS RESPONSE
    """

    try:
        resp = _fetch_single_value(session, sql, (model, prompt, opts_json))
    except (SnowparkSQLException, RuntimeError) as err:
        return None, f"LLM SQL error: {err}"

    if resp is None:
        return None, "LLM error (AI_COMPLETE returned NULL)."

    text: str
    if isinstance(resp, dict):
        text = extract_text(resp) or json.dumps(resp, ensure_ascii=False)
    elif isinstance(resp, str):
        text = resp
        if resp.lstrip().startswith("{"):
            try:
                maybe = json.loads(resp)
            except json.JSONDecodeError:
                maybe = None
            if isinstance(maybe, dict) and maybe.get("choices") is not None:
                extracted = extract_text(maybe)
                if extracted:
                    text = extracted
    else:
        text = str(resp)

    if not text.strip():
        return None, "LLM returned empty content."

    parsed = try_parse_rewrite_payload_details(text)
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
        rewritten_raw = normalize_rewritten_query(text)
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
            return None, "LLM returned an invalid query object."

    if not rewritten:
        return None, "LLM returned no usable text."

    return payload, None


def query_cortex_search_service(
    session: Session,
    *,
    service_fqn: str,
    query: str,
    columns: Sequence[str],
    candidate_limit: int,
    scoring_profile: str | None,
    min_score: float | None,
    tags: Sequence[str] | None = None,
    genres: Sequence[str] | None = None,
    release_year: int | None = None,
    supported_languages: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    parts = service_fqn.split(".")
    if len(parts) != 3:
        raise ValueError("Service FQN must be 'DB.SCHEMA.SERVICE'")
    db_name, schema_name, svc_name = parts

    root = Root(session)
    svc = root.databases[db_name].schemas[schema_name].cortex_search_services[svc_name]

    search_args = _build_search_args(
        query=query,
        columns=columns,
        limit=candidate_limit,
        scoring_profile=scoring_profile,
        tags=tags,
        genres=genres,
        release_year=release_year,
        supported_languages=supported_languages,
    )

    try:
        resp = svc.search(**search_args)
    except (APIError, SnowparkSQLException, ValueError, TypeError) as err:
        raise RuntimeError(
            f"Cortex search failed for service {service_fqn}: {err}"
        ) from err

    rows = resp.results
    # Ensure rows is a list of dicts
    if not isinstance(rows, list):
        rows = []
    rows = [row for row in rows if isinstance(row, dict)]

    if min_score is not None and min_score > 0:
        scores = [row_score(row) for row in rows]
        if any(score is not None for score in scores):
            rows = [
                row
                for row, score in zip(rows, scores, strict=False)
                if score is not None and score >= min_score
            ]

    return rows
