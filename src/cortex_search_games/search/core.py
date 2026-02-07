import ast
import json
from collections.abc import Sequence
from typing import Any

from snowflake.connector import SnowflakeConnection
from snowflake.connector.errors import Error as SnowflakeError
from snowflake.connector.errors import ProgrammingError


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


def _format_tags(row: dict[str, Any]) -> list[str]:
    tags = row.get("TAGS")
    if isinstance(tags, list):
        return [str(v).strip() for v in tags if str(v).strip()]
    if isinstance(tags, str):
        txt = tags.strip()
        if txt.startswith("[") and txt.endswith("]"):
            try:
                parsed = json.loads(txt)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
    return []


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
        str(row.get("SHORT_DESCRIPTION") or ""),
        str(row.get("DETAILED_DESCRIPTION") or ""),
    ]
    tags = _format_tags(row)
    if tags:
        parts.append(" ".join(tags))
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


def _build_search_payload(
    *,
    query: str,
    cols: Sequence[str],
    limit: int,
    profile: str | None,
) -> str:
    if not query.strip():
        raise ValueError("query cannot be empty")
    if limit <= 0:
        raise ValueError("limit must be > 0")

    cols_list = [col.strip() for col in cols if isinstance(col, str) and col.strip()]
    if not cols_list:
        raise ValueError("columns cannot be empty")

    req: dict[str, Any] = {
        "query": query,
        "columns": cols_list,
        "limit": limit,
    }
    if profile:
        req["scoring_profile"] = profile

    return json.dumps(req, ensure_ascii=False)


def _fetch_single_value(
    conn: SnowflakeConnection,
    sql: str,
    params: Sequence[Any] = (),
) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()

    if row is None:
        raise RuntimeError("Snowflake query returned no rows.")

    return row[0]


def ensure_service_exists(conn: SnowflakeConnection, svc: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(f"DESC CORTEX SEARCH SERVICE {svc}")
            cur.fetchone()
    except ProgrammingError as err:
        raise RuntimeError(f"Can't describe Cortex Search Service: {svc}.") from err


def rewrite_query_with_llm(
    conn: SnowflakeConnection,
    *,
    user_query: str,
    model: str,
    temperature: float,
    system_prompt: str,
) -> tuple[str, list[str], str | None]:
    user_prompt = f"User query: {user_query}"
    prompt = f"{system_prompt}\n\n{user_prompt}"
    opts = {"temperature": float(temperature), "max_tokens": 120}

    opts_json = json.dumps(opts, ensure_ascii=False)

    sql = """
    SELECT AI_COMPLETE(
        %s,
        %s,
        PARSE_JSON(%s)
    ) AS RESPONSE
    """

    try:
        resp = _fetch_single_value(conn, sql, (model, prompt, opts_json))
    except (SnowflakeError, RuntimeError) as err:
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
        looks_like_obj = "query" in rewritten.lower() and "{" in rewritten and "}" in rewritten
        if looks_like_obj:
            return user_query, [], "LLM returned an invalid query object."

    if not rewritten:
        return user_query, [], "LLM returned no usable text."

    return rewritten, exclude, None


def query_cortex_search_service(
    conn: SnowflakeConnection,
    *,
    service_fqn: str,
    query: str,
    columns: Sequence[str],
    candidate_limit: int,
    scoring_profile: str | None,
    min_score: float | None,
) -> list[dict[str, Any]]:
    req_json = _build_search_payload(
        query=query,
        cols=columns,
        limit=candidate_limit,
        profile=scoring_profile,
    )

    sql = """
    SELECT PARSE_JSON(
        SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
            %s,
            %s
        )
    )['results'] AS RESULTS
    """

    try:
        raw = _fetch_single_value(conn, sql, (service_fqn, req_json))
    except (SnowflakeError, RuntimeError) as err:
        raise RuntimeError(f"Cortex search failed for service {service_fqn}.") from err

    rows_raw: Any = raw
    if isinstance(raw, str):
        rows_raw = json.loads(raw)

    if not isinstance(rows_raw, list):
        raise RuntimeError("Cortex search returned unexpected result shape.")

    rows = [row for row in rows_raw if isinstance(row, dict)]

    if min_score is not None and min_score > 0:
        scores = [row_score(row) for row in rows]
        if any(score is not None for score in scores):
            rows = [
                row
                for row, score in zip(rows, scores, strict=False)
                if score is not None and score >= min_score
            ]

    return rows
