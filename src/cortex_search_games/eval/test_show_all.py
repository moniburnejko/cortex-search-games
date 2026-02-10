import argparse
from pathlib import Path
from typing import Any

from loguru import logger

from cortex_search_games.eval.batch_test import (
    COLUMNS,
    DEFAULT_SYSTEM_PROMPT,
    LLM_MAX_TOKENS,
    run_batch,
)

SERVICE = "CORTEX_DB.RAW.TEST_DATA_SVC_1_5"
SOURCE_TABLE = "CORTEX_DB.RAW.TEST_DATA"
SCORING_PROFILE = "no_reranker_balanced"
SHOW_ALL_MIN_SCORE = 0.5
SHOW_ALL_LIMIT = 1000
SHOW_ALL_CAND_FACTOR = 1
SHOW_ALL_MAX_CAND = 1000
LLM_MODEL = "claude-4-sonnet"
LLM_TEMP = 0.0

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_FILE = "test_show_all"

CONN_NAME = "cortex"
CONN_TOML: Path | None = None


QUERIES = (
    "I'm in the mood for a good indie game.",
    "Show me action games.",
    "Find adventure games I can dive into.",
    "Give me casual games to relax with.",
    "I only want single-player games.",
    "Can you show me 2D games?",
    "Find indie games, but exclude multiplayer.",
    "Show action games without multiplayer modes.",
    "I'm looking for adventure games, preferably without multiplayer.",
    "Show me indie games with English support released between 2014 and 2016.",
    "Find action games from 2014 to 2016.",
)

SCENARIOS = (
    {
        "name": "show_all",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "use_attribute_filters": True,
    },
)


def run_test_show_all(
    *,
    service: str = SERVICE,
    source_table: str = SOURCE_TABLE,
    scoring_profile: str = SCORING_PROFILE,
    llm_model: str = LLM_MODEL,
    out_dir: Path = OUTPUT_DIR,
    out_file: str = OUTPUT_FILE,
    conn_name: str = CONN_NAME,
    conn_toml: Path | None = CONN_TOML,
) -> dict[str, Any]:
    return run_batch(
        queries=QUERIES,
        cols=COLUMNS,
        profiles=(scoring_profile,),
        score_th=(SHOW_ALL_MIN_SCORE,),
        llm_flags=(True,),
        llm_models=(llm_model,),
        temps=(LLM_TEMP,),
        llm_max_tokens=LLM_MAX_TOKENS,
        svc=service,
        source_table=source_table,
        limit=SHOW_ALL_LIMIT,
        cand_factor=SHOW_ALL_CAND_FACTOR,
        max_cand=SHOW_ALL_MAX_CAND,
        sys_prompt=DEFAULT_SYSTEM_PROMPT,
        scenarios=SCENARIOS,
        out_dir=out_dir,
        out_file=out_file,
        conn_name=conn_name,
        conn_toml=conn_toml,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run show_all-focused batch benchmark using the same logic as "
            "core.py + batch_test.py."
        )
    )
    parser.add_argument("--service", default=SERVICE)
    parser.add_argument("--source-table", default=SOURCE_TABLE)
    parser.add_argument("--scoring-profile", default=SCORING_PROFILE)
    parser.add_argument("--llm-model", default=LLM_MODEL)
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--out-file", default=OUTPUT_FILE)
    parser.add_argument("--conn-name", default=CONN_NAME)
    parser.add_argument("--conn-toml", type=Path, default=CONN_TOML)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        summary = run_test_show_all(
            service=args.service,
            source_table=args.source_table,
            scoring_profile=args.scoring_profile,
            llm_model=args.llm_model,
            out_dir=args.out_dir,
            out_file=args.out_file,
            conn_name=args.conn_name,
            conn_toml=args.conn_toml,
        )
    except (RuntimeError, ValueError) as err:
        logger.error("test_show_all failed: {}", err)
        return 1

    logger.info("test_show_all complete: {}", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
