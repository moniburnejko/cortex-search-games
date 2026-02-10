import csv
from pathlib import Path

from cortex_search_games.eval.batch_test import (
    _combine_query_histories,
    _csv_cell,
    _csv_fieldnames,
    _extract_query_history_metrics,
    _requests_file_path,
    _should_retry_without_attribute_filters,
    _sum_optional_floats,
    _sum_optional_ints,
    _write_rows_csv,
)


def test_extract_query_history_metrics_parses_cost_and_latency() -> None:
    history = {
        "total_elapsed_time": "321",
        "execution_time": 300,
        "compilation_time": "21",
        "bytes_scanned": "1024",
        "credits_used_cloud_services": "0.001",
        "credits_used_compute": 0.0025,
        "credits_used_query_acceleration": 0.0005,
    }

    metrics = _extract_query_history_metrics("01b", history)

    assert metrics["query_id"] == "01b"
    assert metrics["total_elapsed_ms"] == 321
    assert metrics["execution_ms"] == 300
    assert metrics["compilation_ms"] == 21
    assert metrics["bytes_scanned"] == 1024
    assert metrics["cost_cloud_services_credits"] == 0.001
    assert metrics["cost_compute_credits"] == 0.0025
    assert metrics["cost_query_accel_credits"] == 0.0005
    assert metrics["cost_total_credits"] == 0.004


def test_extract_query_history_metrics_uses_warehouse_fallback() -> None:
    history = {
        "TOTAL_ELAPSED_TIME": 120,
        "CREDITS_USED_CLOUD_SERVICES": 0.01,
        "CREDITS_USED_WAREHOUSE": 0.03,
    }

    metrics = _extract_query_history_metrics("q-1", history)

    assert metrics["cost_compute_credits"] == 0.03
    assert metrics["cost_total_credits"] == 0.04


def test_request_helpers_build_expected_values() -> None:
    assert _sum_optional_floats((0.1, None, 0.2), digits=4) == 0.3
    assert _sum_optional_floats((None, None)) is None
    assert _sum_optional_ints((1, None, 2)) == 3
    assert _sum_optional_ints((None, None)) is None
    assert _requests_file_path(Path("output/batch.json")) == Path(
        "output/batch_requests.csv"
    )


def test_csv_helpers_serialize_nested_types_and_keep_column_order() -> None:
    rows = [
        {"a": 1, "b": ["x"], "c": {"k": 7}},
        {"a": 2, "d": "tail"},
    ]
    assert _csv_cell(["x", "y"]) == '["x", "y"]'
    assert _csv_fieldnames(rows) == ["a", "b", "c", "d"]


def test_write_rows_csv_writes_file_for_analysis(tmp_path: Path) -> None:
    rows = [
        {"query": "cats", "excluded_terms": ["dogs"], "meta": {"score": 0.9}},
        {"query": "space", "excluded_terms": [], "latency_ms": 120},
    ]
    out = tmp_path / "batch.csv"
    _write_rows_csv(out, rows)

    with out.open("r", encoding="utf-8", newline="") as f:
        parsed = list(csv.DictReader(f))

    assert len(parsed) == 2
    assert parsed[0]["query"] == "cats"
    assert parsed[0]["excluded_terms"] == '["dogs"]'
    assert parsed[0]["meta"] == '{"score": 0.9}'
    assert parsed[1]["latency_ms"] == "120"


def test_should_retry_without_attribute_filters_only_when_needed() -> None:
    assert (
        _should_retry_without_attribute_filters(
            use_attribute_filters=True,
            tags=["cats"],
            supported_languages=[],
            release_year=None,
            result_count=0,
        )
        is True
    )
    assert (
        _should_retry_without_attribute_filters(
            use_attribute_filters=True,
            tags=[],
            supported_languages=[],
            release_year=None,
            result_count=0,
        )
        is False
    )
    assert (
        _should_retry_without_attribute_filters(
            use_attribute_filters=False,
            tags=["cats"],
            supported_languages=[],
            release_year=None,
            result_count=0,
        )
        is False
    )
    assert (
        _should_retry_without_attribute_filters(
            use_attribute_filters=True,
            tags=["cats"],
            supported_languages=[],
            release_year=None,
            result_count=3,
        )
        is False
    )


def test_combine_query_histories_sums_numeric_metrics() -> None:
    combined = _combine_query_histories(
        ("q1", "q2"),
        (
            {
                "total_elapsed_ms": 100,
                "execution_ms": 60,
                "compilation_ms": 40,
                "bytes_scanned": 1000,
                "cost_cloud_services_credits": 0.01,
                "cost_compute_credits": 0.02,
                "cost_query_accel_credits": 0.0,
            },
            {
                "total_elapsed_ms": 150,
                "execution_ms": 100,
                "compilation_ms": 50,
                "bytes_scanned": 500,
                "cost_cloud_services_credits": 0.02,
                "cost_compute_credits": 0.01,
                "cost_query_accel_credits": 0.003,
            },
        ),
    )

    assert combined["query_id"] == "q1;q2"
    assert combined["total_elapsed_ms"] == 250
    assert combined["execution_ms"] == 160
    assert combined["compilation_ms"] == 90
    assert combined["bytes_scanned"] == 1500
    assert combined["cost_cloud_services_credits"] == 0.03
    assert combined["cost_compute_credits"] == 0.03
    assert combined["cost_query_accel_credits"] == 0.003
    assert combined["cost_total_credits"] == 0.063
