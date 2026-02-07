import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import ijson
from loguru import logger


# DEFAULTS CONFIGURATION
INPUT_PATH = Path("data/games.json")
OUT_DIR = Path("data/games_split")
PARTS = 15
ITEMS_PER_FILE = None
PREFIX = "games_part"
INDENT = None


def _check_split_args(parts: int | None, items_per_file: int | None) -> None:
    if parts is None and items_per_file is None:
        raise ValueError("Set PARTS or ITEMS_PER_FILE.")
    if parts is not None and items_per_file is not None:
        raise ValueError("Use only one: PARTS or ITEMS_PER_FILE.")
    if parts is not None and parts <= 0:
        raise ValueError("PARTS must be > 0.")
    if items_per_file is not None and items_per_file <= 0:
        raise ValueError("ITEMS_PER_FILE must be > 0.")


def count_top_level_keys(path: Path) -> int:
    cnt = 0
    with path.open("rb") as fp:
        for prefix, event, _ in ijson.parse(fp):
            if prefix == "" and event == "map_key":
                cnt += 1
    return cnt


def iter_top_level_items(path: Path) -> Iterator[tuple[str, Any]]:
    with path.open("rb") as fp:
        yield from ijson.kvitems(fp, "")


def resolve_items_per_file(
    path: Path,
    *,
    parts: int | None,
    items_per_file: int | None,
) -> tuple[int, int | None]:
    _check_split_args(parts, items_per_file)

    if items_per_file is not None:
        return items_per_file, None

    assert parts is not None
    total = count_top_level_keys(path)
    return max(1, math.ceil(total / parts)), total


def split_json_object(
    path_in: Path,
    path_out: Path,
    *,
    parts: int | None,
    items_per_file: int | None,
    prefix: str,
    indent: int | None,
) -> int:
    _check_split_args(parts, items_per_file)

    if not path_in.exists():
        raise FileNotFoundError(f"Input file not found: {path_in}")
    if not prefix.strip():
        raise ValueError("PREFIX cannot be empty")
    if indent is not None and indent < 0:
        raise ValueError("INDENT must be >= 0")

    path_out.mkdir(parents=True, exist_ok=True)

    chunk_size, _ = resolve_items_per_file(
        path_in,
        parts=parts,
        items_per_file=items_per_file,
    )
    sep = (",", ":") if indent is None else None

    file_no = 1
    item_no = 0
    first = True
    out_fp = None

    def open_file() -> Any:
        nonlocal file_no, item_no, first
        file_path = path_out / f"{prefix}_{file_no:02d}.json"
        file_no += 1
        item_no = 0
        first = True
        fp = file_path.open("w", encoding="utf-8")
        fp.write("{")
        return fp

    def close_file(fp: Any) -> None:
        fp.write("}\n")
        fp.close()

    for key, val in iter_top_level_items(path_in):
        if out_fp is None:
            out_fp = open_file()

        if item_no >= chunk_size:
            close_file(out_fp)
            out_fp = open_file()

        if not first:
            out_fp.write(",")
        first = False

        out_fp.write(json.dumps(key, ensure_ascii=False))
        out_fp.write(":")
        out_fp.write(
            json.dumps(
                val,
                ensure_ascii=False,
                indent=indent,
                separators=sep,
            )
        )
        item_no += 1

    if out_fp is not None:
        close_file(out_fp)

    return file_no - 1


def main() -> int:
    try:
        file_count = split_json_object(
            INPUT_PATH,
            OUT_DIR,
            parts=PARTS,
            items_per_file=ITEMS_PER_FILE,
            prefix=PREFIX,
            indent=INDENT,
        )
    except (FileNotFoundError, ValueError) as err:
        logger.error("{}", err)
        return 1

    logger.info("Wrote {} files to {}", file_count, OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
