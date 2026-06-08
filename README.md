# cortex-search-games

experiments with snowflake cortex search. the goal is to go past "it returns something" and actually measure search quality: load a dataset into snowflake, stand up a cortex search service, tune scoring profiles, and score the results with a repeatable batch evaluation harness.

built with python (snowpark and streamlit), packaged with uv, and checked in ci with ruff and pytest.

## what's inside

| path | what it does |
|---|---|
| `src/cortex_search_games/search` | query the cortex search service and shape results |
| `src/cortex_search_games/eval` | batch evaluation harness (the `batch-test` entry point) |
| `src/cortex_search_games/utils` | shared helpers |
| `snowflake/` | sql to create the search service and supporting objects |
| `scripts/` | data loading and one-off runs |
| `tests/unit/` | unit tests |

## requirements

- python 3.13+
- a snowflake account with cortex search enabled
- uv for dependency management

## setup

```bash
git clone https://github.com/moniburnejko/cortex-search-games.git
cd cortex-search-games
uv sync
```

configure your snowflake connection (account, user, role, warehouse, database, schema), then run the sql in `snowflake/` to create the search service and supporting objects.

## usage

run the batch evaluation to score search results against your test cases:

```bash
uv run batch-test
```

> note: the eval harness compares cortex search output to expected results, so you can put two scoring profiles side by side and see which one wins.

## development

```bash
uv run ruff check .
uv run pytest
```
