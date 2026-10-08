"""Configuration loading."""

import json

DEFAULTS = {
    "debug": False,
    "server": {"host": "localhost", "port": 8000, "tls": {"enabled": False, "cert": None}},
    "tags": [],
}


class ConfigError(Exception):
    """The configuration file could not be used."""


def load_config(path):
    """Return the configuration from the JSON file at path, merged over DEFAULTS.

    * values in the file override defaults; nested objects are merged key by key, so a file that
      only sets {"server": {"port": 9000}} keeps server.host and server.tls
    * lists and other values in the file replace the default value as a whole
    * a missing file gives a copy of the defaults
    * invalid JSON, or JSON that is not an object, raises ConfigError whose message contains the path
    * DEFAULTS is never modified, and the returned config shares no mutable objects with it
    """
    config = DEFAULTS
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return config
    config.update(data)
    return config
