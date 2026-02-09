import ast
import json
from collections.abc import Sequence
from typing import Any

from snowflake.core import Root
from snowflake.snowpark import Session
from snowflake.snowpark.exceptions import SnowparkSQLException


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


def try_parse_rewrite_payload(text: str) -> tuple[str | None, list[str]]:
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

        query = parsed_dict.get("query")
        if not isinstance(query, str):
            continue

        query = query.strip()
        if not query:
            continue

        exclude_raw = parsed_dict.get("exclude", [])
        exclude: list[str] = []
        if isinstance(exclude_raw, list):
            exclude = [str(v).strip() for v in exclude_raw if str(v).strip()]

        return query, exclude

    return None, []


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
        return user_query, [], f"LLM SQL error: {err}"

    if resp is None:
        return user_query, [], "LLM error (AI_COMPLETE returned NULL)."

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
        return user_query, [], "LLM returned empty content."

    parsed_query, exclude = try_parse_rewrite_payload(text)
    if parsed_query is not None:
        rewritten = normalize_rewritten_query(parsed_query)
    else:
        exclude = []
        rewritten = normalize_rewritten_query(text)
        looks_like_obj = (
            "query" in rewritten.lower() and "{" in rewritten and "}" in rewritten
        )
        if looks_like_obj:
            return user_query, [], "LLM returned an invalid query object."

    if not rewritten:
        return user_query, [], "LLM returned no usable text."

    return rewritten, exclude, None


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
    if not query.strip():
        raise ValueError("query cannot be empty")
    if candidate_limit <= 0:
        raise ValueError("limit must be > 0")

    cols_list = [col.strip() for col in columns if isinstance(col, str) and col.strip()]
    if not cols_list:
        raise ValueError("columns cannot be empty")

    parts = service_fqn.split(".")
    if len(parts) != 3:
        raise ValueError("Service FQN must be 'DB.SCHEMA.SERVICE'")
    db_name, schema_name, svc_name = parts

    root = Root(session)
    svc = root.databases[db_name].schemas[schema_name].cortex_search_services[svc_name]

    search_args = {
        "query": query,
        "columns": cols_list,
        "limit": candidate_limit,
    }

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

    try:
        resp = svc.search(**search_args)
    except Exception as err:
        raise RuntimeError(f"Cortex search failed for service {service_fqn}: {err}") from err

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
