import argparse
import csv
import json
import os
import sys
import time
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

from loguru import logger
from snowflake.snowpark import Session

from cortex_search_games.search.core import (
    ensure_service_exists,
    filter_exclusions,
    query_cortex_search_service,
    resolve_candidate_limit,
    result_scores,
    result_tags,
    rewrite_query_with_llm_details,
    row_score,
)
from cortex_search_games.utils.snowflake_conn import get_session

# DEFAULTS CONFIGURATION

COLUMNS = (
    "NAME",
    "ABOUT_THE_GAME",
    "RELEASE_YEAR",
    "SUPPORTED_LANGUAGES",
    "CATEGORIES",
    "GENRES",
    "TAGS",
)
PROFILES = (
    "balanced_default",
    "keyword_focus",
    "semantic_focus",
    "keyword_extreme",
    "no_reranker_balanced",
    "low_latency",
    "reranker_heavy",
)
SCORE_TH = (0.3, 0.65, 0.75, 0.85)
LLM_MODELS = (
    "claude-4-sonnet",
    "openai-gpt-4.1",)
TEMPS = (0.0, 0.6)
USE_LLM = (False,)
QUERIES = (
    "2025 cozy idle \"desktop\" cat cafe at the bottom of the screen; automation; idler",
    "digital board game: rescue cats tiles into a boat, strategy, sailing theme",
    "early access tile-building worldbuilder, time traveler, far-future cats rule the galaxy",
)

SERVICE = "CORTEX_DB.RAW.CAT_GAMES_SVC_1_5"
SOURCE_TABLE = "CORTEX_DB.RAW.DT_CAT_GAMES"
LIMIT = 10
CAND_FACTOR = 10
MAX_CAND = 500

DEFAULT_SYSTEM_PROMPT = """
You rewrite user queries for hybrid (keyword + vector) search
over a video games catalog.

Return ONLY a valid JSON object with exactly seven keys:
"query", "include_tags", "exclude", "release_year",
"release_year_from", "release_year_to", "supported_languages".

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
  - "release_year_from": an integer year or null
  - "release_year_to": an integer year or null
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
- Extract "release_year_from" and "release_year_to" only when the user asks
  for a year range (e.g. between/from-to). For single-year queries keep both null.
- Extract "supported_languages" only when explicitly requested.

Exclusions and negations:
- If the user explicitly excludes something via negation (no/without/not/avoid/exclude),
  add that term to "exclude".
- "exclude" items should be simple keywords/tags, lowercase, no punctuation.
- Exclusions have priority over the main query and include_tags.

Examples:
Input: User query: co-op multiplayer games set in Japan without cats
Output: {"query":"co-op multiplayer Japan","include_tags":[],"exclude":["cats"],
"release_year":null,"release_year_from":null,"release_year_to":null,
"supported_languages":[]}

Input: User query: battle royale with building, looting resources, and combat
Output: {"query":"battle royale building looting combat","include_tags":[],
"exclude":[],"release_year":null,"release_year_from":null,
"release_year_to":null,"supported_languages":[]}

Input: User query: puzzle game not horror, no gore
Output: {"query":"puzzle","include_tags":[],"exclude":["horror","gore"],
"release_year":null,"release_year_from":null,"release_year_to":null,
"supported_languages":[]}

Input: User query: i want to play something like cyberpunk but with cats, no multiplayer
Output: {"query":"cyberpunk cats futuristic sci-fi single player",
"include_tags":["cyberpunk","cats"],"exclude":["multiplayer"],
"release_year":null,"release_year_from":null,"release_year_to":null,
"supported_languages":[]}

Input: User query: french hidden object game from 2022 without timer
Output: {"query":"hidden object","include_tags":[],"exclude":["timer"],
"release_year":2022,"release_year_from":null,"release_year_to":null,
"supported_languages":["french"]}

Input: User query: show me games released between 2014 and 2016
Output: {"query":"games","include_tags":[],"exclude":[],"release_year":null,
"release_year_from":2014,"release_year_to":2016,"supported_languages":[]}
""".strip()

BASELINE_SYSTEM_PROMPT = """
You rewrite user queries for hybrid (keyword + vector) search
over a video games catalog.

Return ONLY a valid JSON object with exactly two keys: "query" and "exclude".

Hard requirements:
- Output MUST be valid JSON (double quotes, no trailing commas).
- Output MUST be a single JSON object and nothing else.
- Output MUST be a single line.
- Do NOT wrap the JSON in markdown fences/backticks and do NOT add explanations.
- Always include both keys:
  - "query": a string
  - "exclude": an array of strings (use [] if there are no exclusions)
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

Exclusions:
- If the user explicitly excludes something via negation (no/without/not/avoid/exclude),
  add that term to "exclude".
- "exclude" items should be simple keywords/tags, lowercase, no punctuation.
- Exclusions have priority over the main query.
  If a term is in "exclude", it should not appear in "query".
- If there are no exclusions, return "exclude": [].

Examples:
Input: User query: co-op multiplayer games set in Japan without cats
Output: {"query":"co-op multiplayer Japan","exclude":["cats"]}

Input: User query: battle royale with building, looting resources, and combat
Output: {"query":"battle royale building looting combat","exclude":[]}

Input: User query: puzzle game not horror, no gore
Output: {"query":"puzzle","exclude":["horror","gore"]}
""".strip()

SCENARIOS = (
    {
        "name": "default",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "use_attribute_filters": True,
    },
    {
        "name": "baseline",
        "system_prompt": BASELINE_SYSTEM_PROMPT,
        "use_attribute_filters": False,
    },
)
LLM_MAX_TOKENS = 120

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_FILE = "batchtest_emb_15"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "batch_test.log"

CONN_NAME = "cortex"
CONN_TOML: Path | None = None
LOG_LEVEL = "DEBUG"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default

    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


COLLECT_SQL_METRICS = _env_bool("COLLECT_SQL_METRICS", True)


QUERY_HISTORY_SQL = """
SELECT OBJECT_CONSTRUCT(*) AS QUERY_HISTORY
FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(RESULT_LIMIT => 200))
WHERE QUERY_ID = ?
ORDER BY START_TIME DESC
LIMIT 1
"""


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


def setup_log(level: str = LOG_LEVEL, log_file: Path = LOG_FILE) -> Path:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_format = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}"

    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=log_format,
    )
    logger.add(
        log_file,
        level=level.upper(),
        format=log_format,
        encoding="utf-8",
        rotation="10 MB",
        retention=10,
    )
    return log_file


def _to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        txt = val.strip()
        if not txt:
            return None
        try:
            return float(txt)
        except ValueError:
            return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val: Any) -> int | None:
    parsed = _to_float(val)
    if parsed is None:
        return None
    return int(parsed)


def _to_year(val: Any) -> int | None:
    parsed = _to_int(val)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _match_release_year_filters(
    *,
    release_year: int | None,
    release_year_from: int | None,
    release_year_to: int | None,
    known_years: set[int],
) -> tuple[int | None, int | None, int | None]:
    if release_year is not None:
        if not known_years or release_year in known_years:
            return release_year, None, None
        return None, None, None

    year_from = release_year_from
    year_to = release_year_to
    if year_from is None and year_to is None:
        return None, None, None

    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from

    if known_years:
        min_known = min(known_years)
        max_known = max(known_years)

        if year_from is not None:
            year_from = max(year_from, min_known)
            if year_from > max_known:
                year_from = None

        if year_to is not None:
            year_to = min(year_to, max_known)
            if year_to < min_known:
                year_to = None

        if year_from is not None and year_to is not None and year_from > year_to:
            return None, None, None

    if year_from is not None and year_to is not None and year_from == year_to:
        return year_from, None, None

    return None, year_from, year_to


def _meta_get(meta: dict[str, Any], key: str) -> Any:
    for candidate in (key, key.upper(), key.lower()):
        if candidate in meta:
            return meta[candidate]
    return None


def _sum_optional_floats(
    values: tuple[float | None, ...], *, digits: int = 9
) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals), digits)


def _sum_optional_ints(values: tuple[int | None, ...]) -> int | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals)


def _normalize_values(values: list[str]) -> list[str]:
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


def _match_known(values: list[str], known: set[str]) -> tuple[list[str], list[str]]:
    if not values:
        return [], []
    matched: list[str] = []
    missing: list[str] = []
    for val in _normalize_values(values):
        if val.lower() in known:
            matched.append(val)
        else:
            missing.append(val)
    return matched, missing


def _extract_options(session: Session, source_table: str) -> dict[str, Any]:
    tags_sql = f"""
    SELECT DISTINCT TRIM(value::string) AS VAL
    FROM {source_table},
         LATERAL FLATTEN(input => TAGS)
    WHERE value IS NOT NULL
      AND TRIM(value::string) <> ''
    ORDER BY VAL
    """
    langs_sql = f"""
    SELECT DISTINCT TRIM(value::string) AS VAL
    FROM {source_table},
         LATERAL FLATTEN(input => SUPPORTED_LANGUAGES)
    WHERE value IS NOT NULL
      AND TRIM(value::string) <> ''
    ORDER BY VAL
    """
    years_sql = f"""
    SELECT DISTINCT RELEASE_YEAR AS VAL
    FROM {source_table}
    WHERE RELEASE_YEAR IS NOT NULL
    ORDER BY VAL DESC
    """

    tags_rows = session.sql(tags_sql).collect()
    langs_rows = session.sql(langs_sql).collect()
    years_rows = session.sql(years_sql).collect()

    tags = [str(row[0]).strip() for row in tags_rows if row[0] is not None]
    langs = [str(row[0]).strip() for row in langs_rows if row[0] is not None]
    years: list[int] = []
    for row in years_rows:
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
        "tags": _normalize_values(tags),
        "supported_languages": _normalize_values(langs),
        "release_years": sorted(set(years), reverse=True),
    }


def _empty_query_history_metrics(query_id: str) -> dict[str, Any]:
    return {
        "query_id": query_id,
        "total_elapsed_ms": None,
        "execution_ms": None,
        "compilation_ms": None,
        "bytes_scanned": None,
        "cost_cloud_services_credits": None,
        "cost_compute_credits": None,
        "cost_query_accel_credits": None,
        "cost_total_credits": None,
    }


def _extract_query_history_metrics(
    query_id: str, history_row: dict[str, Any]
) -> dict[str, Any]:
    total_elapsed_ms = _to_int(_meta_get(history_row, "TOTAL_ELAPSED_TIME"))
    execution_ms = _to_int(_meta_get(history_row, "EXECUTION_TIME"))
    compilation_ms = _to_int(_meta_get(history_row, "COMPILATION_TIME"))
    bytes_scanned = _to_int(_meta_get(history_row, "BYTES_SCANNED"))

    cloud_credits = _to_float(_meta_get(history_row, "CREDITS_USED_CLOUD_SERVICES"))
    compute_credits = _to_float(_meta_get(history_row, "CREDITS_USED_COMPUTE"))
    if compute_credits is None:
        compute_credits = _to_float(_meta_get(history_row, "CREDITS_USED_WAREHOUSE"))
    query_accel_credits = _to_float(
        _meta_get(history_row, "CREDITS_USED_QUERY_ACCELERATION")
    )
    total_credits = _sum_optional_floats(
        (
            cloud_credits,
            compute_credits,
            query_accel_credits,
        )
    )

    return {
        "query_id": query_id,
        "total_elapsed_ms": total_elapsed_ms,
        "execution_ms": execution_ms,
        "compilation_ms": compilation_ms,
        "bytes_scanned": bytes_scanned,
        "cost_cloud_services_credits": cloud_credits,
        "cost_compute_credits": compute_credits,
        "cost_query_accel_credits": query_accel_credits,
        "cost_total_credits": total_credits,
    }


def _last_query_id(session: Session) -> str:
    try:
        rows = session.sql("SELECT LAST_QUERY_ID()").collect()
        row = rows[0] if rows else None
    except Exception as err:
        logger.debug("Couldn't fetch LAST_QUERY_ID(): {}", err)
        return ""

    if row is None or row[0] in (None, ""):
        return ""
    return str(row[0])


def _query_history_metrics(
    session: Session,
    query_id: str,
    *,
    attempts: int = 3,
    wait_seconds: float = 0.2,
) -> dict[str, Any]:
    if not query_id:
        return _empty_query_history_metrics("")

    for attempt in range(attempts):
        try:
            rows = session.sql(QUERY_HISTORY_SQL, params=[query_id]).collect()
            row = rows[0] if rows else None
        except Exception as err:
            logger.debug("QUERY_HISTORY_BY_SESSION failed for {}: {}", query_id, err)
            return _empty_query_history_metrics(query_id)

        if row and row[0]:
            payload = row[0]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = None
            if isinstance(payload, dict):
                return _extract_query_history_metrics(query_id, payload)

        if attempt < attempts - 1:
            time.sleep(wait_seconds)

    return _empty_query_history_metrics(query_id)


def _combine_query_histories(
    query_ids: tuple[str, ...], histories: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    combined_query_id = ";".join(qid for qid in query_ids if qid)
    if not histories:
        return _empty_query_history_metrics(combined_query_id)

    total_elapsed_ms = _sum_optional_ints(
        tuple(_to_int(history.get("total_elapsed_ms")) for history in histories)
    )
    execution_ms = _sum_optional_ints(
        tuple(_to_int(history.get("execution_ms")) for history in histories)
    )
    compilation_ms = _sum_optional_ints(
        tuple(_to_int(history.get("compilation_ms")) for history in histories)
    )
    bytes_scanned = _sum_optional_ints(
        tuple(_to_int(history.get("bytes_scanned")) for history in histories)
    )
    cloud_credits = _sum_optional_floats(
        tuple(
            _to_float(history.get("cost_cloud_services_credits"))
            for history in histories
        )
    )
    compute_credits = _sum_optional_floats(
        tuple(_to_float(history.get("cost_compute_credits")) for history in histories)
    )
    query_accel_credits = _sum_optional_floats(
        tuple(
            _to_float(history.get("cost_query_accel_credits")) for history in histories
        )
    )
    total_credits = _sum_optional_floats(
        (
            cloud_credits,
            compute_credits,
            query_accel_credits,
        )
    )

    return {
        "query_id": combined_query_id,
        "total_elapsed_ms": total_elapsed_ms,
        "execution_ms": execution_ms,
        "compilation_ms": compilation_ms,
        "bytes_scanned": bytes_scanned,
        "cost_cloud_services_credits": cloud_credits,
        "cost_compute_credits": compute_credits,
        "cost_query_accel_credits": query_accel_credits,
        "cost_total_credits": total_credits,
    }


def _should_retry_without_attribute_filters(
    *,
    use_attribute_filters: bool,
    tags: list[str],
    supported_languages: list[str],
    release_year: int | None,
    release_year_from: int | None = None,
    release_year_to: int | None = None,
    result_count: int,
) -> bool:
    if not use_attribute_filters:
        return False
    if result_count > 0:
        return False
    return bool(
        tags
        or supported_languages
        or release_year is not None
        or release_year_from is not None
        or release_year_to is not None
    )


def _requests_file_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.stem}_requests.csv")


def _csv_cell(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _csv_fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key in seen:
                continue
            seen.add(key)
            names.append(key)
    return names


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = _csv_fieldnames(rows)
    if not fieldnames:
        path.write_text("", encoding="utf-8")
        return

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_cell(row.get(k)) for k in fieldnames})


# CONFIG CHECKS AND ITERATORS


def _check_cfg(
    queries: tuple[str, ...],
    cols: tuple[str, ...],
    profiles: tuple[str, ...],
    score_th: tuple[float, ...],
    llm_flags: tuple[bool, ...],
    llm_models: tuple[str, ...],
    temps: tuple[float, ...],
    llm_max_tokens: int,
    limit: int,
    cand_factor: int,
    max_cand: int,
) -> None:
    if not queries:
        raise ValueError("queries cannot be empty")
    if not cols:
        raise ValueError("columns cannot be empty")
    if not profiles:
        raise ValueError("profiles cannot be empty")
    if not score_th:
        raise ValueError("score_th cannot be empty")
    if not llm_flags:
        raise ValueError("llm_flags cannot be empty")
    if limit <= 0:
        raise ValueError("limit must be > 0")
    if cand_factor <= 0:
        raise ValueError("cand_factor must be > 0")
    if max_cand <= 0:
        raise ValueError("max_cand must be > 0")
    if llm_max_tokens <= 0:
        raise ValueError("llm_max_tokens must be > 0")

    if any(v < 0.0 or v > 1.0 for v in score_th):
        raise ValueError("score_th values must be in [0.0, 1.0]")

    if any(v < 0.0 or v > 1.0 for v in temps):
        raise ValueError("temps values must be in [0.0, 1.0]")

    if True in llm_flags:
        if not llm_models:
            raise ValueError("llm_models cannot be empty when USE_LLM has True")
        if not temps:
            raise ValueError("temps cannot be empty when USE_LLM has True")


def iter_cfgs(
    profiles: tuple[str, ...],
    score_th: tuple[float, ...],
    llm_flags: tuple[bool, ...],
    llm_models: tuple[str, ...],
    temps: tuple[float, ...],
) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []

    for use_llm in llm_flags:
        models = llm_models if use_llm else (None,)
        cfg_temps = temps if use_llm else (None,)

        for model, temp, profile, min_score in product(
            models,
            cfg_temps,
            profiles,
            score_th,
        ):
            out.append(
                {
                    "use_llm": use_llm,
                    "model": model,
                    "temp": temp,
                    "profile": profile,
                    "min_score": min_score,
                }
            )

    return tuple(out)


# MAIN BATCH LOGIC


def run_batch(
    queries: tuple[str, ...] = QUERIES,
    cols: tuple[str, ...] = COLUMNS,
    profiles: tuple[str, ...] = PROFILES,
    score_th: tuple[float, ...] = SCORE_TH,
    llm_flags: tuple[bool, ...] = USE_LLM,
    llm_models: tuple[str, ...] = LLM_MODELS,
    temps: tuple[float, ...] = TEMPS,
    llm_max_tokens: int = LLM_MAX_TOKENS,
    svc: str = SERVICE,
    source_table: str = SOURCE_TABLE,
    limit: int = LIMIT,
    cand_factor: int = CAND_FACTOR,
    max_cand: int = MAX_CAND,
    sys_prompt: str = DEFAULT_SYSTEM_PROMPT,
    scenarios: tuple[dict[str, Any], ...] | None = None,
    out_dir: Path = OUTPUT_DIR,
    out_file: str = OUTPUT_FILE,
    conn_name: str = CONN_NAME,
    conn_toml: Path | None = CONN_TOML,
) -> dict[str, Any]:
    _check_cfg(
        queries,
        cols,
        profiles,
        score_th,
        llm_flags,
        llm_models,
        temps,
        llm_max_tokens,
        limit,
        cand_factor,
        max_cand,
    )

    if scenarios is None:
        scenarios = SCENARIOS
    if not scenarios:
        raise ValueError("scenarios cannot be empty")

    with get_session(conn_name, conn_toml) as session:
        ensure_service_exists(session, svc)

        cand_limit = resolve_candidate_limit(
            limit=limit,
            candidate_factor=cand_factor,
            max_candidates=max_cand,
        )

        options: dict[str, Any] | None = None
        tags_known: set[str] = set()
        langs_known: set[str] = set()
        years_known: set[int] = set()
        if any(bool(scenario.get("use_attribute_filters")) for scenario in scenarios):
            options = _extract_options(session, source_table)
            tags_known = {tag.lower() for tag in options["tags"]}
            langs_known = {lang.lower() for lang in options["supported_languages"]}
            years_known = set(options["release_years"])

        cfgs = iter_cfgs(profiles, score_th, llm_flags, llm_models, temps)
        req_count = 0
        rows: list[dict[str, Any]] = []
        request_rows: list[dict[str, Any]] = []

        logger.debug(
            "Batch start: queries={} cfgs={} cand_limit={}",
            len(queries),
            len(cfgs),
            cand_limit,
        )

        for q in queries:
            for scenario in scenarios:
                for cfg in cfgs:
                    req_count += 1
                    rew_q = q
                    rew_err = ""
                    rew_ms = 0
                    rewrite_query_id = ""
                    rewrite_history = _empty_query_history_metrics("")
                    search_query_id = ""
                    search_history = _empty_query_history_metrics("")

                    exclude_terms: list[str] = []
                    include_tags_raw: list[str] = []
                    include_langs_raw: list[str] = []
                    year_int: int | None = None
                    year_from_int: int | None = None
                    year_to_int: int | None = None
                    matched_tags: list[str] = []
                    unknown_tags: list[str] = []
                    matched_langs: list[str] = []
                    unknown_langs: list[str] = []
                    matched_year: int | None = None
                    matched_year_from: int | None = None
                    matched_year_to: int | None = None

                    if cfg["use_llm"]:
                        t0 = time.perf_counter()
                        try:
                            rewrite, err = rewrite_query_with_llm_details(
                                session,
                                user_query=q,
                                model=cfg["model"] or "",
                                temperature=cfg["temp"] or 0.0,
                                max_tokens=llm_max_tokens,
                                system_prompt=str(
                                    scenario.get("system_prompt") or sys_prompt
                                ),
                            )
                        except Exception as e:
                            logger.error(f"LLM Rewrite failed for query '{q}': {e}")
                            rewrite, err = None, str(e)

                        if rewrite is None:
                            rewrite = {
                                "query": q,
                                "exclude": [],
                                "include_tags": [],
                                "release_year": None,
                                "release_year_from": None,
                                "release_year_to": None,
                                "supported_languages": [],
                            }

                        rew_q = str(rewrite.get("query") or q)
                        exclude_terms = _normalize_values(
                            [str(v).lower() for v in list(rewrite.get("exclude") or [])]
                        )
                        include_tags_raw = _normalize_values(
                            list(rewrite.get("include_tags") or [])
                        )
                        include_langs_raw = _normalize_values(
                            list(rewrite.get("supported_languages") or [])
                        )
                        year_int = _to_year(rewrite.get("release_year"))
                        year_from_int = _to_year(rewrite.get("release_year_from"))
                        year_to_int = _to_year(rewrite.get("release_year_to"))

                        rew_ms = round((time.perf_counter() - t0) * 1000)
                        rew_err = err or ""
                        if COLLECT_SQL_METRICS:
                            rewrite_query_id = _last_query_id(session)
                            rewrite_history = _query_history_metrics(
                                session, rewrite_query_id
                            )

                    use_attr = bool(scenario.get("use_attribute_filters"))
                    if use_attr and options is not None:
                        matched_tags, unknown_tags = _match_known(
                            include_tags_raw, tags_known
                        )
                        matched_langs, unknown_langs = _match_known(
                            include_langs_raw, langs_known
                        )
                        matched_year, matched_year_from, matched_year_to = (
                            _match_release_year_filters(
                                release_year=year_int,
                                release_year_from=year_from_int,
                                release_year_to=year_to_int,
                                known_years=years_known,
                            )
                        )

                    requested_attr_tags = matched_tags if use_attr else []
                    requested_attr_langs = matched_langs if use_attr else []
                    requested_attr_year = matched_year if use_attr else None
                    requested_attr_year_from = matched_year_from if use_attr else None
                    requested_attr_year_to = matched_year_to if use_attr else None
                    applied_attr_tags = requested_attr_tags
                    applied_attr_langs = requested_attr_langs
                    applied_attr_year = requested_attr_year
                    applied_attr_year_from = requested_attr_year_from
                    applied_attr_year_to = requested_attr_year_to
                    attr_filters_relaxed = False

                    t1 = time.perf_counter()
                    results = query_cortex_search_service(
                        session,
                        service_fqn=svc,
                        query=rew_q,
                        columns=cols,
                        candidate_limit=cand_limit,
                        scoring_profile=cfg["profile"],
                        min_score=cfg["min_score"],
                        tags=applied_attr_tags,
                        supported_languages=applied_attr_langs,
                        release_year=applied_attr_year,
                        release_year_from=applied_attr_year_from,
                        release_year_to=applied_attr_year_to,
                    )
                    if not isinstance(results, list):
                        results = list(results)
                    search_query_ids: list[str] = []
                    search_histories: list[dict[str, Any]] = []
                    if COLLECT_SQL_METRICS:
                        query_id = _last_query_id(session)
                        search_query_ids.append(query_id)
                        search_histories.append(
                            _query_history_metrics(session, query_id)
                        )

                    if _should_retry_without_attribute_filters(
                        use_attribute_filters=use_attr,
                        tags=applied_attr_tags,
                        supported_languages=applied_attr_langs,
                        release_year=applied_attr_year,
                        release_year_from=applied_attr_year_from,
                        release_year_to=applied_attr_year_to,
                        result_count=len(results),
                    ):
                        attr_filters_relaxed = True
                        applied_attr_tags = []
                        applied_attr_langs = []
                        applied_attr_year = None
                        applied_attr_year_from = None
                        applied_attr_year_to = None

                        results = query_cortex_search_service(
                            session,
                            service_fqn=svc,
                            query=rew_q,
                            columns=cols,
                            candidate_limit=cand_limit,
                            scoring_profile=cfg["profile"],
                            min_score=cfg["min_score"],
                            tags=applied_attr_tags,
                            supported_languages=applied_attr_langs,
                            release_year=applied_attr_year,
                            release_year_from=applied_attr_year_from,
                            release_year_to=applied_attr_year_to,
                        )
                        if not isinstance(results, list):
                            results = list(results)

                        if COLLECT_SQL_METRICS:
                            query_id = _last_query_id(session)
                            search_query_ids.append(query_id)
                            search_histories.append(
                                _query_history_metrics(session, query_id)
                            )

                    results = filter_exclusions(results, exclude_terms)
                    search_ms = round((time.perf_counter() - t1) * 1000)
                    search_query_id = ";".join(qid for qid in search_query_ids if qid)
                    search_history = _combine_query_histories(
                        tuple(search_query_ids), tuple(search_histories)
                    )

                    ts = datetime.now(UTC).isoformat(timespec="seconds")
                    request_cost_credits = _sum_optional_floats(
                        (
                            _to_float(rewrite_history["cost_total_credits"]),
                            _to_float(search_history["cost_total_credits"]),
                        )
                    )
                    request_total_elapsed_ms = _sum_optional_ints(
                        (
                            _to_int(rewrite_history["total_elapsed_ms"]),
                            _to_int(search_history["total_elapsed_ms"]),
                        )
                    )
                    request_common: dict[str, Any] = {
                        "timestamp_utc": ts,
                        "service": svc,
                        "scenario": scenario.get("name") or "",
                        "original_query": q,
                        "rewritten_query": rew_q,
                        "excluded_terms": exclude_terms,
                        "rewrite_error": rew_err,
                        "use_llm": cfg["use_llm"],
                        "llm_model": cfg["model"] or "",
                        "llm_max_tokens": llm_max_tokens if cfg["use_llm"] else "",
                        "temperature": cfg["temp"] if cfg["temp"] is not None else "",
                        "scoring_profile": cfg["profile"] or "",
                        "min_score": cfg["min_score"],
                        "limit": limit,
                        "candidate_limit": cand_limit,
                        "rewrite_ms": rew_ms,
                        "search_ms": search_ms,
                        "request_ms": rew_ms + search_ms,
                        "rewrite_query_id": rewrite_query_id,
                        "search_query_id": search_query_id,
                        "rewrite_total_elapsed_ms": rewrite_history["total_elapsed_ms"],
                        "search_total_elapsed_ms": search_history["total_elapsed_ms"],
                        "request_total_elapsed_ms": request_total_elapsed_ms,
                        "rewrite_cost_total_credits": rewrite_history[
                            "cost_total_credits"
                        ],
                        "search_cost_total_credits": search_history[
                            "cost_total_credits"
                        ],
                        "request_cost_total_credits": request_cost_credits,
                        "rewrite_cost_cloud_services_credits": rewrite_history[
                            "cost_cloud_services_credits"
                        ],
                        "search_cost_cloud_services_credits": search_history[
                            "cost_cloud_services_credits"
                        ],
                        "rewrite_cost_compute_credits": rewrite_history[
                            "cost_compute_credits"
                        ],
                        "search_cost_compute_credits": search_history[
                            "cost_compute_credits"
                        ],
                        "rewrite_cost_query_accel_credits": rewrite_history[
                            "cost_query_accel_credits"
                        ],
                        "search_cost_query_accel_credits": search_history[
                            "cost_query_accel_credits"
                        ],
                        "include_tags_raw": include_tags_raw,
                        "include_tags_matched": matched_tags,
                        "include_tags_unknown": unknown_tags,
                        "supported_languages_raw": include_langs_raw,
                        "supported_languages_matched": matched_langs,
                        "supported_languages_unknown": unknown_langs,
                        "release_year_raw": year_int,
                        "release_year_from_raw": year_from_int,
                        "release_year_to_raw": year_to_int,
                        "release_year_matched": matched_year,
                        "release_year_from_matched": matched_year_from,
                        "release_year_to_matched": matched_year_to,
                        "attribute_filters_enabled": use_attr,
                        "attribute_filters_relaxed": attr_filters_relaxed,
                        "attribute_filter_tags_requested": requested_attr_tags,
                        "attribute_filter_supported_languages_"
                        "requested": requested_attr_langs,
                        "attribute_filter_release_year_requested": requested_attr_year,
                        "attribute_filter_release_year_from_requested": (
                            requested_attr_year_from
                        ),
                        "attribute_filter_release_year_to_requested": (
                            requested_attr_year_to
                        ),
                        "attribute_filter_tags": applied_attr_tags,
                        "attribute_filter_supported_languages": applied_attr_langs,
                        "attribute_filter_release_year": applied_attr_year,
                        "attribute_filter_release_year_from": applied_attr_year_from,
                        "attribute_filter_release_year_to": applied_attr_year_to,
                    }

                    request_rows.append(
                        request_common
                        | {
                            "result_count": len(results),
                            "result_count_after_limit": len(results[:limit]),
                        }
                    )

                    for rank, row in enumerate(results[:limit], start=1):
                        rows.append(
                            request_common
                            | {
                                "result_rank": rank,
                                "result_name": row.get("NAME"),
                                "result_score": row_score(row),
                                "release_year": row.get("RELEASE_YEAR"),
                                "supported_languages": ";".join(
                                    _format_str_list(row.get("SUPPORTED_LANGUAGES"))
                                ),
                                "categories": ";".join(
                                    _format_str_list(row.get("CATEGORIES"))
                                ),
                                "genres": ";".join(_format_str_list(row.get("GENRES"))),
                                "tags": ";".join(result_tags(row)),
                                "about_the_game": (row.get("ABOUT_THE_GAME") or "")[
                                    :500
                                ],
                                "scores_json": json.dumps(
                                    result_scores(row) or {},
                                    ensure_ascii=False,
                                ),
                            }
                        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_base_path = out_dir / out_file
    out_csv_path = (
        out_base_path
        if out_base_path.suffix.lower() == ".csv"
        else out_base_path.with_suffix(".csv")
    )
    requests_csv_path = _requests_file_path(out_csv_path)
    _write_rows_csv(out_csv_path, rows)
    _write_rows_csv(requests_csv_path, request_rows)

    return {
        "total_queries": len(queries),
        "total_request_configs": req_count,
        "request_rows_written": len(request_rows),
        "rows_written": len(rows),
        "local_results_csv_file": str(out_csv_path),
        "local_requests_csv_file": str(requests_csv_path),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run batch evaluation with selectable LLM prompt scenario.",
    )
    parser.add_argument(
        "--scenario",
        choices=("default", "baseline", "all"),
        default="default",
        help=(
            "Which prompt scenario to run. "
            "'default' uses the filter-aware prompt from streamlit_snow.py, "
            "'baseline' uses the non-filter-aware prompt, "
            "'all' runs both."
        ),
    )
    return parser.parse_args()


def main() -> int:
    log_file = setup_log()
    logger.info("Logging batch output to {}", log_file)

    try:
        args = _parse_args()
        if args.scenario == "all":
            scenarios = SCENARIOS
        else:
            scenarios = tuple(
                scenario for scenario in SCENARIOS if scenario["name"] == args.scenario
            )
        suffix = args.scenario
        out_file = f"{OUTPUT_FILE}_{suffix}"
        summary = run_batch(scenarios=scenarios, out_file=out_file)
    except (RuntimeError, ValueError) as err:
        logger.error("Batch run failed: {}", err)
        return 1

    logger.info("Batch test complete: {}", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
