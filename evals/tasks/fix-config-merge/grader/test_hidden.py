import copy
import json

import pytest

import config as config_module
from config import DEFAULTS, ConfigError, load_config

PRISTINE = copy.deepcopy(DEFAULTS)


def write(tmp_path, data):
    path = tmp_path / "c.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_deeply_nested_merge(tmp_path):
    config = load_config(write(tmp_path, {"server": {"tls": {"enabled": True}}}))
    assert config["server"]["tls"] == {"enabled": True, "cert": None}
    assert config["server"]["host"] == "localhost"
    assert config["debug"] is False


def test_lists_are_replaced_not_merged(tmp_path):
    assert load_config(write(tmp_path, {"tags": ["a", "b"]}))["tags"] == ["a", "b"]


def test_new_keys_are_kept(tmp_path):
    assert load_config(write(tmp_path, {"extra": 1}))["extra"] == 1


def test_defaults_are_never_modified(tmp_path):
    load_config(write(tmp_path, {"server": {"port": 1}, "debug": True, "tags": ["x"]}))
    assert config_module.DEFAULTS == PRISTINE
    again = load_config(tmp_path / "missing.json")
    assert again["server"]["port"] == 8000 and again["debug"] is False and again["tags"] == []


def test_result_shares_nothing_with_defaults(tmp_path):
    config = load_config(tmp_path / "missing.json")
    config["server"]["tls"]["enabled"] = True
    config["tags"].append("x")
    assert config_module.DEFAULTS == PRISTINE
    assert load_config(tmp_path / "missing.json")["server"]["tls"]["enabled"] is False


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"just a string"', "42"])
def test_bad_files_raise_config_error_with_the_path(tmp_path, text):
    path = write(tmp_path, text)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert str(path) in str(excinfo.value)


def test_file_may_replace_a_dict_with_a_scalar(tmp_path):
    assert load_config(write(tmp_path, {"server": None}))["server"] is None
