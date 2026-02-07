import json
import pytest

from cortex_search_games.utils.split_json import split_json_object


def test_split_json_object_with_parts(tmp_path) -> None:
    src = {
        "game_1": {"name": "A"},
        "game_2": {"name": "B"},
        "game_3": {"name": "C"},
        "game_4": {"name": "D"},
        "game_5": {"name": "E"},
    }

    in_path = tmp_path / "games.json"
    in_path.write_text(json.dumps(src), encoding="utf-8")

    out_dir = tmp_path / "parts"
    count = split_json_object(
        in_path,
        out_dir,
        parts=2,
        items_per_file=None,
        prefix="chunk",
        indent=None,
    )

    assert count == 2
    files = sorted(out_dir.glob("chunk_*.json"))
    assert [p.name for p in files] == ["chunk_01.json", "chunk_02.json"]

    merged: dict[str, object] = {}
    for path in files:
        merged.update(json.loads(path.read_text(encoding="utf-8")))

    assert merged == src


def test_split_json_object_requires_single_strategy(tmp_path) -> None:
    in_path = tmp_path / "games.json"
    in_path.write_text("{}", encoding="utf-8")

    out_dir = tmp_path / "out"

    with pytest.raises(ValueError):
        split_json_object(
            in_path,
            out_dir,
            parts=None,
            items_per_file=None,
            prefix="chunk",
            indent=None,
        )

    with pytest.raises(ValueError):
        split_json_object(
            in_path,
            out_dir,
            parts=2,
            items_per_file=10,
            prefix="chunk",
            indent=None,
        )
