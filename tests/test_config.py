import os
from unittest.mock import patch

import config


def test_data_path_move_writes_config_and_restart_pointer(tmp_path, monkeypatch):
    current = tmp_path / "current"
    default = tmp_path / "default"
    destination = tmp_path / "moved"
    current.mkdir()
    monkeypatch.setattr(config, "CONFIG_FILE", current / "config.json")
    monkeypatch.setattr(config, "_DEFAULT_DATA_DIR", default)
    monkeypatch.setattr(config, "_DATA_POINTER_FILE", default / "data-location.json")

    config.save_config({"data_path": str(destination), "output_path": "songs"})

    assert (destination / "config.json").is_file()
    pointer = config._load_json_safe(default / "data-location.json", lambda: {})
    assert pointer["data_path"] == str(destination.resolve())


def test_server_mode_uses_persistent_secret_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    env = {"USDB_SERVER_MODE": "1"}

    with patch.dict(os.environ, env, clear=False):
        config.set_cookie("PHPSESSID=server-cookie")
        config.set_login_credentials("usdb-user", "usdb-password")

        assert config.get_cookie() == "PHPSESSID=server-cookie"
        assert config.get_login_credentials() == ("usdb-user", "usdb-password")

        config.set_cookie("")
        assert config.get_cookie() == ""

    assert (tmp_path / "server-secrets.json").exists()
