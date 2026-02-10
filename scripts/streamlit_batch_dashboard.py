import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output"
QUERY_SETS_PATH = PROJECT_ROOT / "src/cortex_search_games/eval/queries_cats.md"
REQUESTS_SUFFIX = "_requests.csv"

REQUEST_KEY_FALLBACK_COLS = [
    "timestamp_utc",
    "original_query",
    "rewritten_query",
    "use_llm",
    "llm_model",
    "temperature",
    "scoring_profile",
    "min_score",
]


@dataclass(frozen=True)
class RunFiles:
    run_name: str
    requests_csv: Path
    results_csv: Path


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_query_lookup_key(value: Any) -> str:
    text = _normalize_text(value).lower()
    if not text:
        return ""

    text = (
        text.replace("—", "-")
        .replace("–", "-")
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
    )
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split()).strip()


def _parse_list_field(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []

    text = str(value).strip()
    if not text:
        return []

    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(text)
        except (json.JSONDecodeError, ValueError, SyntaxError):
            continue
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]

    return []


def _parse_query_tuple(code_block: str) -> tuple[str, ...]:
    match = re.search(r"QUERIES\s*=\s*\((?P<body>.*)\)", code_block, re.DOTALL)
    if match is None:
        return ()

    tuple_text = f"({match.group('body')})"
    try:
        parsed = ast.literal_eval(tuple_text)
    except (SyntaxError, ValueError):
        return ()

    if isinstance(parsed, tuple):
        return tuple(str(item).strip() for item in parsed if str(item).strip())
    if isinstance(parsed, list):
        return tuple(str(item).strip() for item in parsed if str(item).strip())
    return ()


def _extract_query_sets(markdown_text: str) -> dict[str, list[str]]:
    query_sets: dict[str, list[str]] = {}
    current_heading = ""
    in_code_block = False
    code_lines: list[str] = []

    def _append_query_mapping(query: str, heading: str) -> None:
        normalized_exact = _normalize_text(query).lower()
        normalized_lookup = _normalize_query_lookup_key(query)
        for key in (normalized_exact, normalized_lookup):
            if not key:
                continue
            mapped = query_sets.setdefault(key, [])
            if heading not in mapped:
                mapped.append(heading)

    for line in markdown_text.splitlines():
        stripped = line.strip()
        if not in_code_block and stripped:
            heading_match = re.match(r"^\s*(?:#+\s*|\d+\.\s*)(.+?)\s*$", stripped)
            if heading_match:
                current_heading = heading_match.group(1).strip()
            elif not stripped.startswith("```"):
                current_heading = stripped

        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_lines = []
                continue

            in_code_block = False
            if not current_heading:
                continue

            queries = _parse_query_tuple("\n".join(code_lines))
            for query in queries:
                if not _normalize_text(query):
                    continue
                _append_query_mapping(query, current_heading)
            continue

        if in_code_block:
            code_lines.append(line)

    return query_sets


@st.cache_data(show_spinner=False)
def _load_query_set_mapping(
    path: str,
    cache_buster: float | None = None,
) -> dict[str, list[str]]:
    query_sets_path = Path(path)
    if not query_sets_path.exists():
        return {}
    return _extract_query_sets(query_sets_path.read_text(encoding="utf-8"))


def _primary_query_set(set_names: list[str]) -> str:
    if not set_names:
        return "unmapped"

    specific_sets = [
        set_name
        for set_name in set_names
        if "all queries together" not in set_name.lower()
    ]
    if specific_sets:
        return specific_sets[0]
    return set_names[0]


def _discover_runs(output_dir: Path) -> list[RunFiles]:
    if not output_dir.exists():
        return []

    runs: list[RunFiles] = []
    for request_csv in output_dir.glob(f"*{REQUESTS_SUFFIX}"):
        run_name = request_csv.name[: -len(REQUESTS_SUFFIX)]
        results_csv = output_dir / f"{run_name}.csv"
        runs.append(
            RunFiles(
                run_name=run_name,
                requests_csv=request_csv,
                results_csv=results_csv,
            )
        )
    runs = [run for run in runs if run.results_csv.exists()]

    runs.sort(
        key=lambda run: run.requests_csv.stat().st_mtime,
        reverse=True,
    )
    return runs


@st.cache_data(show_spinner=False)
def _load_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path)


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None

    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _request_key(df: pd.DataFrame) -> pd.Series:
    pieces: list[pd.Series] = []
    for col in REQUEST_KEY_FALLBACK_COLS:
        if col in df.columns:
            pieces.append(df[col].fillna("").astype(str))
        else:
            pieces.append(pd.Series("", index=df.index))

    fallback = pieces[0]
    for piece in pieces[1:]:
        fallback = fallback + "||" + piece

    if "search_query_id" in df.columns:
        query_ids = df["search_query_id"].fillna("").astype(str).str.strip()
    else:
        query_ids = pd.Series("", index=df.index)

    return query_ids.where(query_ids != "", "fallback::" + fallback)


def _temperature_label(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "(none)"
    text = str(value).strip()
    if not text:
        return "(none)"
    try:
        return f"{float(text):.2f}"
    except ValueError:
        return text


def _prepare_requests(
    df: pd.DataFrame,
    *,
    run_name: str,
    query_set_mapping: dict[str, list[str]],
) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    out["run_name"] = run_name
    out["request_key"] = _request_key(out)
    out["use_llm"] = out["use_llm"].apply(_coerce_bool)

    numeric_cols = [
        "temperature",
        "min_score",
        "limit",
        "candidate_limit",
        "rewrite_ms",
        "search_ms",
        "request_ms",
        "request_total_elapsed_ms",
        "rewrite_cost_total_credits",
        "search_cost_total_credits",
        "request_cost_total_credits",
        "rewrite_cost_cloud_services_credits",
        "search_cost_cloud_services_credits",
        "rewrite_cost_compute_credits",
        "search_cost_compute_credits",
        "rewrite_cost_query_accel_credits",
        "search_cost_query_accel_credits",
        "result_count",
        "result_count_after_limit",
    ]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    for col in ("rewrite_query_id", "search_query_id"):
        if col in out.columns:
            out[col] = out[col].fillna("").astype(str).str.strip()
        else:
            out[col] = ""

    if "timestamp_utc" in out.columns:
        out["timestamp_utc"] = pd.to_datetime(
            out["timestamp_utc"], errors="coerce", utc=True
        )

    if "excluded_terms" in out.columns:
        out["excluded_terms_list"] = out["excluded_terms"].apply(_parse_list_field)
        out["excluded_terms_text"] = out["excluded_terms_list"].apply(", ".join)
    else:
        out["excluded_terms_list"] = [[] for _ in range(len(out))]
        out["excluded_terms_text"] = ""

    if "rewrite_error" in out.columns:
        rewrite_error = out["rewrite_error"].fillna("").astype(str).str.strip()
        out["rewrite_error_flag"] = rewrite_error != ""
    else:
        out["rewrite_error_flag"] = False

    if "llm_model" in out.columns:
        model = out["llm_model"].fillna("").astype(str).str.strip()
        out["llm_model_display"] = model.where(model != "", "(none)")
    else:
        out["llm_model_display"] = "(none)"

    if "temperature" in out.columns:
        out["temperature_label"] = out["temperature"].apply(_temperature_label)
    else:
        out["temperature_label"] = "(none)"

    if "original_query" in out.columns:
        normalized_exact = (
            out["original_query"].fillna("").map(_normalize_text).str.lower()
        )
        normalized_lookup = (
            out["original_query"].fillna("").map(_normalize_query_lookup_key)
        )
        out["query_sets"] = [
            query_set_mapping.get(exact, query_set_mapping.get(lookup, []))
            for exact, lookup in zip(normalized_exact, normalized_lookup, strict=False)
        ]
        out["query_set_primary"] = out["query_sets"].map(_primary_query_set)
        out["query_set_all"] = out["query_sets"].map(", ".join)
    else:
        out["query_sets"] = [[] for _ in range(len(out))]
        out["query_set_primary"] = "unmapped"
        out["query_set_all"] = ""

    return out


def _prepare_results(df: pd.DataFrame, *, run_name: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    out["run_name"] = run_name
    out["request_key"] = _request_key(out)

    numeric_cols = [
        "result_rank",
        "result_score",
        "request_ms",
        "search_ms",
        "rewrite_ms",
    ]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    if "timestamp_utc" in out.columns:
        out["timestamp_utc"] = pd.to_datetime(
            out["timestamp_utc"], errors="coerce", utc=True
        )

    return out


def _safe_options(series: pd.Series) -> list[str]:
    values = sorted(
        {
            str(value).strip()
            for value in series.dropna().astype(str)
            if str(value).strip()
        }
    )
    return values


def _format_maybe_number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and pd.isna(value):
        return "n/a"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{numeric:.{digits}f}"


def _request_label(row: pd.Series) -> str:
    query = _normalize_text(row.get("original_query", ""))
    if len(query) > 90:
        query = f"{query[:87]}..."

    timestamp = row.get("timestamp_utc")
    if isinstance(timestamp, pd.Timestamp) and not pd.isna(timestamp):
        ts_label = timestamp.isoformat()
    else:
        ts_label = "n/a"

    return (
        f"[{row.get('run_name', 'run')}] {ts_label} | "
        f"{row.get('scoring_profile', '')} | "
        f"use_llm={row.get('use_llm', '')} | "
        f"min_score={row.get('min_score', '')} | "
        f"{query}"
    )


def _config_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    group_cols = [
        "scoring_profile",
        "use_llm",
        "llm_model_display",
        "temperature_label",
        "min_score",
    ]
    group_cols = [col for col in group_cols if col in df.columns]

    agg_spec: dict[str, tuple[str, str]] = {
        "requests": ("request_key", "nunique"),
        "avg_request_ms": ("request_ms", "mean"),
        "avg_search_ms": ("search_ms", "mean"),
        "avg_results_after_limit": ("result_count_after_limit", "mean"),
        "rewrite_error_rate": ("rewrite_error_flag", "mean"),
    }
    if "request_cost_total_credits" in df.columns:
        agg_spec["avg_cost_credits"] = ("request_cost_total_credits", "mean")

    summary = df.groupby(group_cols, dropna=False).agg(**agg_spec).reset_index()
    if "avg_cost_credits" not in summary.columns:
        summary["avg_cost_credits"] = pd.NA
    summary["rewrite_error_rate"] = summary["rewrite_error_rate"] * 100.0
    return summary.sort_values("avg_request_ms", ascending=True)


def _load_selected_runs(
    selected_runs: list[RunFiles],
    query_set_mapping: dict[str, list[str]],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    request_parts: list[pd.DataFrame] = []
    result_parts: list[pd.DataFrame] = []

    for run in selected_runs:
        request_df = _load_csv(str(run.requests_csv))
        request_parts.append(
            _prepare_requests(
                request_df,
                run_name=run.run_name,
                query_set_mapping=query_set_mapping,
            )
        )

        result_df = _load_csv(str(run.results_csv))
        result_parts.append(_prepare_results(result_df, run_name=run.run_name))

    requests = (
        pd.concat(request_parts, ignore_index=True) if request_parts else pd.DataFrame()
    )
    results = (
        pd.concat(result_parts, ignore_index=True) if result_parts else pd.DataFrame()
    )
    return requests, results


def _apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    filtered = df.copy()

    query_sets = _safe_options(filtered["query_set_primary"])
    selected_query_sets = st.sidebar.multiselect(
        "Query set",
        options=query_sets,
        default=query_sets,
    )
    if selected_query_sets:
        filtered = filtered[filtered["query_set_primary"].isin(selected_query_sets)]

    query_text = st.sidebar.text_input("Query contains", value="")
    if query_text.strip():
        lowered = query_text.strip().lower()
        mask = (
            filtered["original_query"]
            .fillna("")
            .str.lower()
            .str.contains(lowered, regex=False)
        )
        filtered = filtered[mask]

    use_llm_filter = st.sidebar.radio(
        "Use LLM",
        options=["all", "true", "false"],
        horizontal=False,
    )
    if use_llm_filter == "true":
        filtered = filtered[filtered["use_llm"].eq(True)]
    elif use_llm_filter == "false":
        filtered = filtered[filtered["use_llm"].eq(False)]

    model_options = _safe_options(filtered["llm_model_display"])
    selected_models = st.sidebar.multiselect(
        "LLM model",
        options=model_options,
        default=model_options,
    )
    if selected_models:
        filtered = filtered[filtered["llm_model_display"].isin(selected_models)]

    profile_options = _safe_options(filtered["scoring_profile"])
    selected_profiles = st.sidebar.multiselect(
        "Scoring profile",
        options=profile_options,
        default=profile_options,
    )
    if selected_profiles:
        filtered = filtered[filtered["scoring_profile"].isin(selected_profiles)]

    temperature_options = _safe_options(filtered["temperature_label"])
    selected_temperatures = st.sidebar.multiselect(
        "Temperature",
        options=temperature_options,
        default=temperature_options,
    )
    if selected_temperatures:
        filtered = filtered[filtered["temperature_label"].isin(selected_temperatures)]

    score_values = filtered["min_score"].dropna()
    if not score_values.empty:
        min_score = float(score_values.min())
        max_score = float(score_values.max())
        if abs(max_score - min_score) < 1e-12:
            selected_range = (min_score, max_score)
            st.sidebar.caption(
                f"Min score range is fixed at {min_score:.2f} for this run."
            )
        else:
            selected_range = st.sidebar.slider(
                "Min score range",
                min_value=min_score,
                max_value=max_score,
                value=(min_score, max_score),
                step=0.01,
            )
        filtered = filtered[
            filtered["min_score"].between(selected_range[0], selected_range[1])
        ]

    rewrite_error_filter = st.sidebar.radio(
        "Rewrite error",
        options=["all", "with error", "no error"],
        horizontal=False,
    )
    if "rewrite_error_flag" in filtered.columns:
        if rewrite_error_filter == "with error":
            filtered = filtered[filtered["rewrite_error_flag"]]
        elif rewrite_error_filter == "no error":
            filtered = filtered[~filtered["rewrite_error_flag"]]

    return filtered


def _show_metrics_collection_status(df: pd.DataFrame) -> None:
    has_query_id_col = "search_query_id" in df.columns
    has_cost_col = "request_cost_total_credits" in df.columns

    if not has_query_id_col and not has_cost_col:
        st.info(
            "Loaded file schema does not include SQL metrics columns "
            "(`search_query_id`, `request_cost_total_credits`)."
        )
        return

    has_query_ids = (
        df["search_query_id"].fillna("").astype(str).str.strip().ne("").any()
        if has_query_id_col
        else False
    )
    has_costs = (
        df["request_cost_total_credits"].notna().any() if has_cost_col else False
    )
    if not has_query_ids and not has_costs:
        st.info(
            "This run has no collected SQL metrics. Re-run batch with "
            "`COLLECT_SQL_METRICS=true` to populate `search_query_id` and credits."
        )


def _show_metrics(df: pd.DataFrame) -> None:
    request_count = len(df)
    unique_queries = df["original_query"].nunique(dropna=True)
    avg_request_ms = df["request_ms"].mean()
    avg_results = df["result_count_after_limit"].mean()
    avg_cost = (
        df["request_cost_total_credits"].mean()
        if "request_cost_total_credits" in df.columns
        else None
    )
    rows_with_cost = (
        int(df["request_cost_total_credits"].notna().sum())
        if "request_cost_total_credits" in df.columns
        else 0
    )
    rows_with_search_id = (
        int(df["search_query_id"].fillna("").astype(str).str.strip().ne("").sum())
        if "search_query_id" in df.columns
        else 0
    )

    cols = st.columns(7)
    cols[0].metric("Requests", f"{request_count}")
    cols[1].metric("Unique queries", f"{unique_queries}")
    cols[2].metric("Avg request ms", _format_maybe_number(avg_request_ms, digits=0))
    cols[3].metric("Avg results", _format_maybe_number(avg_results, digits=2))
    cols[4].metric("Avg credits", _format_maybe_number(avg_cost, digits=6))
    cols[5].metric("Rows with cost", f"{rows_with_cost}/{request_count}")
    cols[6].metric(
        "Rows with search_query_id", f"{rows_with_search_id}/{request_count}"
    )


def _show_charts(df: pd.DataFrame) -> None:
    st.subheader("Latency by scoring profile")
    chart_df = (
        df.groupby(["scoring_profile", "use_llm"], dropna=False)["request_ms"]
        .mean()
        .reset_index()
    )
    chart_df["use_llm"] = (
        chart_df["use_llm"].map({True: "LLM", False: "No LLM"}).fillna("Unknown")
    )
    pivot = (
        chart_df.pivot(index="scoring_profile", columns="use_llm", values="request_ms")
        .fillna(0.0)
        .sort_index()
    )
    st.bar_chart(pivot)

    st.subheader("Average returned results by query set")
    query_set_chart = (
        df.groupby("query_set_primary", dropna=False)["result_count_after_limit"]
        .mean()
        .sort_values(ascending=False)
    )
    st.bar_chart(query_set_chart)


def _show_request_details(requests: pd.DataFrame, results: pd.DataFrame) -> None:
    st.subheader("Request details")

    sorted_requests = requests.sort_values(
        "timestamp_utc", ascending=False
    ).reset_index(drop=True)
    selected_idx = st.selectbox(
        "Choose request",
        options=list(range(len(sorted_requests))),
        format_func=lambda idx: _request_label(sorted_requests.iloc[idx]),
    )
    selected = sorted_requests.iloc[selected_idx]

    detail = {
        "run_name": selected.get("run_name"),
        "timestamp_utc": (
            selected["timestamp_utc"].isoformat()
            if isinstance(selected.get("timestamp_utc"), pd.Timestamp)
            else selected.get("timestamp_utc")
        ),
        "query_set_primary": selected.get("query_set_primary"),
        "query_set_all": selected.get("query_set_all"),
        "original_query": selected.get("original_query"),
        "rewritten_query": selected.get("rewritten_query"),
        "excluded_terms": selected.get("excluded_terms_list"),
        "include_tags_raw": selected.get("include_tags_raw"),
        "include_tags_matched": selected.get("include_tags_matched"),
        "include_tags_unknown": selected.get("include_tags_unknown"),
        "supported_languages_raw": selected.get("supported_languages_raw"),
        "supported_languages_matched": selected.get("supported_languages_matched"),
        "supported_languages_unknown": selected.get("supported_languages_unknown"),
        "release_year_raw": selected.get("release_year_raw"),
        "release_year_matched": selected.get("release_year_matched"),
        "attribute_filters_enabled": selected.get("attribute_filters_enabled"),
        "attribute_filter_tags": selected.get("attribute_filter_tags"),
        "attribute_filter_supported_languages": selected.get(
            "attribute_filter_supported_languages"
        ),
        "attribute_filter_release_year": selected.get("attribute_filter_release_year"),
        "rewrite_error": selected.get("rewrite_error"),
        "use_llm": selected.get("use_llm"),
        "llm_model": selected.get("llm_model_display"),
        "temperature": selected.get("temperature_label"),
        "scoring_profile": selected.get("scoring_profile"),
        "min_score": selected.get("min_score"),
        "result_count_after_limit": selected.get("result_count_after_limit"),
        "request_ms": selected.get("request_ms"),
        "request_cost_total_credits": selected.get("request_cost_total_credits"),
        "search_query_id": selected.get("search_query_id"),
    }
    st.json(detail)

    if results.empty:
        st.info("No matching results CSV loaded for selected run.")
        return

    mask = (results["run_name"] == selected["run_name"]) & (
        results["request_key"] == selected["request_key"]
    )
    request_results = results[mask].sort_values("result_rank")

    if request_results.empty:
        st.warning("No result rows matched this request key.")
        return

    columns = [
        "result_rank",
        "result_name",
        "result_score",
        "release_year",
        "categories",
        "genres",
        "tags",
        "supported_languages",
        "about_the_game",
    ]
    existing = [col for col in columns if col in request_results.columns]
    st.dataframe(
        request_results[existing],
        width="stretch",
        hide_index=True,
    )


def main() -> None:
    st.set_page_config(
        page_title="Cortex Search Batch Dashboard",
        page_icon=":bar_chart:",
        layout="wide",
    )
    st.title("Cortex Search Batch Dashboard")
    st.caption(
        "Interactive exploration of batch outputs from "
        "`src/cortex_search_games/eval/batch_test.py`."
    )

    runs = _discover_runs(OUTPUT_DIR)
    if not runs:
        st.error(
            f"No files found in `{OUTPUT_DIR}`. "
            "Expected paired CSV files: `<run>.csv` and `<run>_requests.csv`."
        )
        return

    st.sidebar.header("Data source")
    run_names = [run.run_name for run in runs]
    selected_names = st.sidebar.multiselect(
        "Runs",
        options=run_names,
        default=run_names,
    )
    if not selected_names:
        st.warning("Select at least one run.")
        return

    selected_runs = [run for run in runs if run.run_name in selected_names]
    query_sets_path = Path(QUERY_SETS_PATH)
    query_sets_mtime = (
        query_sets_path.stat().st_mtime if query_sets_path.exists() else None
    )
    query_set_mapping = _load_query_set_mapping(
        str(query_sets_path),
        cache_buster=query_sets_mtime,
    )

    with st.spinner("Loading selected runs..."):
        requests, results = _load_selected_runs(selected_runs, query_set_mapping)

    if requests.empty:
        st.error("Selected runs do not contain request rows.")
        return

    st.sidebar.header("Filters")
    if query_set_mapping:
        st.sidebar.caption(
            f"Query set mapping loaded: {len(query_set_mapping)} keys "
            f"from `{QUERY_SETS_PATH.name}`."
        )
    else:
        st.sidebar.warning("Query set mapping is empty.")
    filtered = _apply_filters(requests)
    if filtered.empty:
        st.warning("No rows match current filters.")
        return

    _show_metrics_collection_status(filtered)
    if "query_set_primary" in filtered.columns:
        unmapped_count = int((filtered["query_set_primary"] == "unmapped").sum())
        if unmapped_count:
            unique_unmapped = (
                filtered.loc[
                    filtered["query_set_primary"] == "unmapped", "original_query"
                ]
                .dropna()
                .astype(str)
                .unique()
            )
            sample = ", ".join(list(unique_unmapped[:3]))
            st.warning(
                f"Unmapped queries: {unmapped_count}. Sample: {sample}"
                if sample
                else f"Unmapped queries: {unmapped_count}."
            )
    _show_metrics(filtered)

    st.subheader("Filtered requests")
    table_cols = [
        "run_name",
        "timestamp_utc",
        "query_set_primary",
        "use_llm",
        "llm_model_display",
        "temperature_label",
        "scoring_profile",
        "min_score",
        "request_ms",
        "result_count_after_limit",
        "request_cost_total_credits",
        "search_query_id",
        "original_query",
        "rewritten_query",
        "excluded_terms_text",
    ]
    existing_table_cols = [col for col in table_cols if col in filtered.columns]
    st.dataframe(
        filtered[existing_table_cols].sort_values("timestamp_utc", ascending=False),
        width="stretch",
        hide_index=True,
    )

    st.subheader("Configuration comparison")
    summary = _config_summary(filtered)
    st.dataframe(summary, width="stretch", hide_index=True)

    _show_charts(filtered)
    _show_request_details(filtered, results)


if __name__ == "__main__":
    main()
