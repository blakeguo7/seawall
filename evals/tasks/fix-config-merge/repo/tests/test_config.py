import json

from config import DEFAULTS, load_config


def test_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.json") == DEFAULTS


def test_nested_override_keeps_siblings(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"server": {"port": 9000}}))
    config = load_config(path)
    assert config["server"]["port"] == 9000
    assert config["server"]["host"] == "localhost"
