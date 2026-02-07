import ast
import json
from typing import Any

import streamlit as st
from snowflake.snowpark.context import get_active_session
from snowflake.snowpark.exceptions import SnowparkSQLException

# DEFAULTS CONFIGURATION

PAGE_TITLE = "Find your new fave game"
PAGE_ICON = "🎮"
APP_TITLE = "🎮 Find your new fave game 🎮"
APP_CAPTION = "Semantic search over the Steam games catalog (Snowflake Cortex Search)."
QUERY_PLACEHOLDER = "e.g. battle royale with building, looting resources, and combat"

DB = "CORTEX_DB"
SCHEMA = "RAW"
SERVICE = "GAMES_SVC"
SERVICE_FQN = f"{DB}.{SCHEMA}.{SERVICE}"

RETURN_COLS = ["NAME", "SHORT_DESCRIPTION", "DETAILED_DESCRIPTION", "TAGS"]
MIN_SCORE = 0.3

DEFAULT_SYSTEM_PROMPT = """
You rewrite user queries for semantic search over a video games catalog.
Return ONLY a JSON object with two keys: "query" and "exclude".
- "query" is the rewritten search query (English, keyword-rich).
- "exclude" is a list of keywords/tags that the user explicitly excludes
  (based on negations like "no", "without", "not", "exclude", "avoid").
Preserve user-provided tags/keywords (do not drop them).
If the query is already good, return it unchanged.
If you cannot improve it, return the original user query unchanged.
Do NOT invent game titles. Focus on genres, mechanics, themes, and features.
Example: {"query": "battle royale building survival shooting multiplayer", "exclude": []}
Example: {"query": "co-op sci-fi shooter space aliens", "exclude": ["cats"]}
""".strip()

LLM_MODELS = ["claude-4-sonnet", "openai-gpt-5-chat", "llama3.1-70b"]
SCORING_OPTIONS = ["balanced_default", "keyword_focus", "low_latency"]
DEFAULT_SCORING = "balanced_default"


# HELPER FUNCTIONS

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


def _try_parse_payload(text: str) -> tuple[str | None, list[str]]:
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


def _tags(row: dict[str, Any]) -> list[str]:
    tags = row.get("TAGS") or []
    if not isinstance(tags, list):
        return []
    return [str(v).strip() for v in tags if str(v).strip()]


def _scores(row: dict[str, Any]) -> dict[str, Any] | None:
    scores = row.get("@scores")
    return scores if isinstance(scores, dict) else None


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


def _row_debug_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "@scores": _scores(row),
        "NAME": row.get("NAME"),
        "DETAILED_DESCRIPTION": row.get("DETAILED_DESCRIPTION"),
        "TAGS": _format_tags(row),
    }


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


def _filter_exclusions(rows: list[dict[str, Any]], exclude: list[str]) -> list[dict[str, Any]]:
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
    sess = get_active_session()
    try:
        sess.sql(f"DESC CORTEX SEARCH SERVICE {SERVICE_FQN}").collect()
    except SnowparkSQLException as err:
        st.error(f"Can't describe Cortex Search Service: `{SERVICE_FQN}`.")
        st.exception(err)
        st.stop()


# MAIN LOGIC

def query_service(
    query: str,
    cand_limit: int,
    scoring: str | None,
    min_score: float | None,
) -> list[dict[str, Any]]:
    sess = get_active_session()

    req = {
        "query": query,
        "columns": RETURN_COLS,
        "limit": cand_limit,
    }
    if scoring:
        req["scoring_profile"] = scoring

    req_json = _escape_sql(json.dumps(req, ensure_ascii=False))

    sql = f"""
    SELECT PARSE_JSON(
        SNOWFLAKE.CORTEX.SEARCH_PREVIEW(
            '{SERVICE_FQN}',
            '{req_json}'
        )
    )['results'] AS RESULTS
    """

    raw = sess.sql(sql).collect()[0][0]
    rows = json.loads(raw) if isinstance(raw, str) else raw

    if not isinstance(rows, list):
        return []

    out = [row for row in rows if isinstance(row, dict)]
    if min_score is not None and min_score > 0:
        scores = [_row_score(row) for row in out]
        if any(score is not None for score in scores):
            out = [
                row
                for row, score in zip(out, scores, strict=False)
                if score is not None and score >= min_score
            ]

    return out


def rewrite_query(
    user_query: str,
    model: str,
    temp: float,
) -> tuple[str, list[str], str | None]:
    sess = get_active_session()
    user_prompt = f"User query: {user_query}"
    prompt = f"{DEFAULT_SYSTEM_PROMPT}\n\n{user_prompt}"

    opts = {"temperature": float(temp), "max_tokens": 120}
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
        return user_query, [], f"LLM SQL error: {err}"

    if resp is None:
        return user_query, [], "LLM error (AI_COMPLETE returned NULL)."

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
        return user_query, [], "LLM returned empty content."

    parsed_query, exclude = _try_parse_payload(text)
    if parsed_query is not None:
        rewritten = _normalize_query(parsed_query)
    else:
        exclude = []
        rewritten = _normalize_query(text)
        looks_like_obj = "query" in rewritten.lower() and "{" in rewritten and "}" in rewritten
        if looks_like_obj:
            return user_query, [], "LLM returned an invalid query object."

    if not rewritten:
        return user_query, [], "LLM returned no usable text."

    return rewritten, exclude, None


def render_form() -> dict[str, Any]:
    with st.form("search_form"):
        query = st.text_input(
            "Describe the game you want to find",
            placeholder=QUERY_PLACEHOLDER,
            key="query",
        )
        limit = int(
            st.number_input("Results to show", min_value=1, value=10, key="limit")
        )
        cand_limit = min(500, max(50, limit * 10))

        with st.expander("LLM rewrite (optional)", expanded=False):
            use_llm = st.checkbox("Rewrite query with LLM", value=True, key="use_llm")
            llm_model = st.selectbox(
                "LLM model", options=LLM_MODELS, index=0, key="llm_model"
            )
            llm_temp = st.slider(
                "LLM temperature (lower = predictable, higher = diverse)",
                min_value=0.0,
                max_value=0.6,
                value=0.0,
                step=0.05,
                key="llm_temperature",
            )

        with st.expander("Ranking tuning (optional)", expanded=False):
            scoring = st.selectbox(
                "Scoring profile",
                options=SCORING_OPTIONS,
                index=SCORING_OPTIONS.index(DEFAULT_SCORING),
                key="scoring_profile",
                format_func=lambda v: v if v else "Default service ranking",
            )
            min_score = st.slider(
                "Minimum score threshold (0 = no filtering)",
                min_value=0.0,
                max_value=1.0,
                value=MIN_SCORE,
                step=0.05,
                key="min_score",
            )

        submitted = st.form_submit_button(
            "Search", type="primary", use_container_width=True
        )

    return {
        "query": query,
        "limit": limit,
        "cand_limit": cand_limit,
        "use_llm": use_llm,
        "llm_model": llm_model,
        "llm_temp": llm_temp,
        "scoring": scoring,
        "min_score": min_score,
        "submitted": submitted,
    }


def show_results(
    rows: list[dict[str, Any]],
    limit: int,
    cand_limit: int,
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
    elif rows:
        sources = {_score_source(row) for row in rows}

    for idx, row in enumerate(rows[:limit], start=1):
        name = row.get("NAME")
        short = row.get("SHORT_DESCRIPTION") or ""
        detailed = row.get("DETAILED_DESCRIPTION") or ""
        tags = _tags(row)
        score = _row_score(row)
        score_parts = _scores(row)

        with st.container():
            st.markdown(f"### {idx}. {name}")

            if short:
                excerpt = short if len(short) <= 400 else f"{short[:400].rstrip()}..."
                st.write(excerpt)
                if detailed and detailed.strip() and detailed != short:
                    with st.expander("See detailed description", expanded=False):
                        st.write(detailed)
            elif detailed:
                excerpt = (
                    detailed
                    if len(detailed) <= 400
                    else f"{detailed[:400].rstrip()}..."
                )
                st.write(excerpt)
                if len(detailed) > 400:
                    with st.expander("See more", expanded=False):
                        st.write(detailed)

            if tags:
                st.caption(f"Tags: {', '.join(tags)}")
            with st.expander("Ranking components (@scores)", expanded=False):
                if score is not None:
                    st.write(f"Score: {score:.3f}")
                else:
                    st.write("Score: n/a")
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
    form = render_form()

    if not form["submitted"]:
        if not form["query"]:
            st.info("Enter a query and click **Search** to find games!")
        return

    q = (form["query"] or "").strip()
    if not q:
        st.warning("Enter a query")
        return

    exclude_terms: list[str] = []
    if form["use_llm"]:
        with st.spinner("Rewriting your query for better results..."):
            rewritten, exclude_terms, err = rewrite_query(
                q, form["llm_model"], form["llm_temp"]
            )
        if err:
            st.warning(f"LLM: {err} Using the original query")
        else:
            q = rewritten
            with st.expander("LLM rewritten query", expanded=False):
                st.code(q)
            if exclude_terms:
                st.caption(f"Excluded terms: {', '.join(exclude_terms)}")

    rows = query_service(
        q,
        form["cand_limit"],
        form["scoring"] or None,
        form["min_score"],
    )
    rows = _filter_exclusions(rows, exclude_terms)
    show_results(rows, form["limit"], form["cand_limit"])


if __name__ == "__main__":
    main()
