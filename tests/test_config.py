import json
from pathlib import Path

import pytest

from archiver.config import load_config


def test_load_config_reads_guild_id_and_data_dir(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"guild_id": "123456789012345678", "data_dir": "D:/archive"}),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.guild_id == "123456789012345678"
    assert config.data_dir == Path("D:/archive")


def test_load_config_missing_key_raises(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"guild_id": "123"}), encoding="utf-8")
    with pytest.raises(ValueError, match="data_dir"):
        load_config(config_path)


def test_load_config_coerces_numeric_guild_id_to_string(tmp_path: Path):
    """config.json authored by hand might have an unquoted numeric guild_id;
    the catalog always stores/compares IDs as TEXT (spec §4)."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"guild_id": 123456789012345678, "data_dir": "D:/archive"}),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.guild_id == "123456789012345678"
    assert isinstance(config.guild_id, str)
