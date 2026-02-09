import csv
import json
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
    rewrite_query_with_llm,
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
    "openai-gpt-4.1",
    "mixtral-8x7b",
)
TEMPS = (0.0, 0.6)
USE_LLM = (False, True)
QUERIES = (
    "Is \"Hidden Cats in Krakow\" in the catalog? if not, show closest hidden cats city games",
    "Cat Quest IV - if it's not, show me similar pirate cat action RPG (NOT Cat Quest II)",
    "battle royale cat game set on Mars, but NOT shooting, NOT multiplayer, NOT violence",
    "cozy cat cafe management sim, but NOT a visual novel, NOT anime, avoid dating sim",
    "third-person cat adventure in a city, no horror, no gore",
    "hidden object cats in a city, without timer, exclude leaderboard, avoid time attack",
    "neon cybercity cat adventure with a drone companion, stealthy exploration, mysterious robots",
    "a lone cat in a neon, decaying city of robots; exploration, stealth, mystery (3rd person)",
    "catventure: open-world 2D action RPG with cats and dogs, local co-op, loot and spells",
)

SERVICE = "CORTEX_DB.RAW.CAT_GAMES_SVC_1_5"
LIMIT = 10
CAND_FACTOR = 10
MAX_CAND = 500

DEFAULT_SYSTEM_PROMPT = """
You rewrite user queries for hybrid (keyword + vector) search over a video games catalog.

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
- "query" must be short, English, keyword-rich, suitable for hybrid (keyword + vector) search.
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
- Exclusions have priority over the main query. If a term is in "exclude", it should not appear in "query".
- If there are no exclusions, return "exclude": [].

Examples:
Input: User query: co-op multiplayer games set in Japan without cats
Output: {"query":"co-op multiplayer Japan","exclude":["cats"]}

Input: User query: battle royale with building, looting resources, and combat
Output: {"query":"battle royale building looting combat","exclude":[]}

Input: User query: puzzle game not horror, no gore
Output: {"query":"puzzle","exclude":["horror","gore"]}
""".strip()

SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
LLM_MAX_TOKENS = 120

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_FILE = "test_emb_1_5_p1"

CONN_NAME = "cortex"
CONN_TOML: Path | None = None
LOG_LEVEL = "DEBUG"
COLLECT_SQL_METRICS = False


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


def setup_log(level: str = LOG_LEVEL) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )


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


def _requests_file_path(out_path: Path) -> Path:
    suffix = out_path.suffix or ".json"
    return out_path.with_name(f"{out_path.stem}_requests{suffix}")


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
    limit: int = LIMIT,
    cand_factor: int = CAND_FACTOR,
    max_cand: int = MAX_CAND,
    sys_prompt: str = SYSTEM_PROMPT,
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

    with get_session(conn_name, conn_toml) as session:
        ensure_service_exists(session, svc)

        cand_limit = resolve_candidate_limit(
            limit=limit,
            candidate_factor=cand_factor,
            max_candidates=max_cand,
        )

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
                if cfg["use_llm"]:
                    t0 = time.perf_counter()
                    try:
                        rew_q, exclude_terms, err = rewrite_query_with_llm(
                            session,
                            user_query=q,
                            model=cfg["model"] or "",
                            temperature=cfg["temp"] or 0.0,
                            max_tokens=llm_max_tokens,
                            system_prompt=sys_prompt,
                        )
                    except Exception as e:
                        logger.error(f"LLM Rewrite failed for query '{q}': {e}")
                        rew_q, exclude_terms, err = q, [], str(e)

                    rew_ms = round((time.perf_counter() - t0) * 1000)
                    rew_err = err or ""
                    if COLLECT_SQL_METRICS:
                        rewrite_query_id = _last_query_id(session)
                        rewrite_history = _query_history_metrics(
                            session, rewrite_query_id
                        )

                t1 = time.perf_counter()
                results = query_cortex_search_service(
                    session,
                    service_fqn=svc,
                    query=rew_q,
                    columns=cols,
                    candidate_limit=cand_limit,
                    scoring_profile=cfg["profile"],
                    min_score=cfg["min_score"],
                )
                if not isinstance(results, list):
                    results = list(results)

                results = filter_exclusions(results, exclude_terms)
                search_ms = round((time.perf_counter() - t1) * 1000)
                if COLLECT_SQL_METRICS:
                    search_query_id = _last_query_id(session)
                    search_history = _query_history_metrics(session, search_query_id)

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
                    "rewrite_cost_total_credits": rewrite_history["cost_total_credits"],
                    "search_cost_total_credits": search_history["cost_total_credits"],
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
                            "about_the_game": (row.get("ABOUT_THE_GAME") or "")[:500],
                            "scores_json": json.dumps(
                                result_scores(row) or {},
                                ensure_ascii=False,
                            ),
                        }
                    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / out_file
    requests_path = _requests_file_path(out_path)
    out_csv_path = out_path.with_suffix(".csv")
    requests_csv_path = _requests_file_path(out_csv_path)
    out_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    requests_path.write_text(
        json.dumps(request_rows, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _write_rows_csv(out_csv_path, rows)
    _write_rows_csv(requests_csv_path, request_rows)

    return {
        "total_queries": len(queries),
        "total_request_configs": req_count,
        "request_rows_written": len(request_rows),
        "rows_written": len(rows),
        "local_file": str(out_path),
        "local_requests_file": str(requests_path),
        "local_csv_file": str(out_csv_path),
        "local_requests_csv_file": str(requests_csv_path),
    }


def main() -> int:
    setup_log()

    try:
        summary = run_batch()
    except (RuntimeError, ValueError) as err:
        logger.error("Batch run failed: {}", err)
        return 1

    logger.info("Batch test complete: {}", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
