"""Selected fleet storage and credentials override the adapter's configured fallbacks."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))
import runtime_paths as nw_paths


def load_bus():
    spec = importlib.util.spec_from_file_location("isolated_bus", ROOT / "scripts/agent-bus-v3.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "schema": "fleet-runtime/v1",
        "runtime_dir": str(tmp_path / "default-state"),
        "paths": {"lock_directory": str(tmp_path / "default-locks"), "lock_prefix": "old-"},
        "bus": {"config_directory": str(tmp_path / "default-bus"),
                "database": str(tmp_path / "default-inbox.sqlite3")},
        "matrix": {"token_file": str(tmp_path / "default-auth.hdr")},
    }))
    return {"HOME": str(tmp_path), "FLEET_ORCHESTRATOR_CONFIG": str(path)}


def test_named_matrix_profile_keeps_its_own_token_and_database(tmp_path):
    selected = tmp_path / "named-matrix"
    selected.mkdir()
    (selected / "auth.hdr").write_text("Authorization: Bearer synthetic-named-value")
    env = {**settings(tmp_path), "NW_FLEET": "sample", "NW_FLEET_PROFILE_APPLIED": "sample",
           "MATRIX_BUS_CFG": str(selected)}
    with patch.dict(os.environ, env, clear=True):
        bus = load_bus()
        assert bus.DB_PATH == selected / "agent-bus-v3.sqlite3"
        assert bus.auth_header_path() == selected / "auth.hdr"
        assert bus.auth_token() == "synthetic-named-value"


def test_explicit_bus_directory_keeps_old_database_and_auth_semantics(tmp_path):
    selected = tmp_path / "selected-bus"
    with patch.dict(os.environ, {**settings(tmp_path), "AGENT_BUS_CFG": str(selected)}, clear=True):
        bus = load_bus()
        assert bus.CFG == selected
        assert bus.DB_PATH == selected / "agent-bus-v3.sqlite3"
        assert bus.auth_header_path() == selected / "auth.hdr"


def test_explicit_database_takes_precedence_over_selected_directory(tmp_path):
    database = tmp_path / "custom-inbox.sqlite3"
    with patch.dict(os.environ, {**settings(tmp_path), "MATRIX_BUS_CFG": str(tmp_path / "selected"),
                                 "AGENT_BUS_DB": str(database)}, clear=True):
        assert load_bus().DB_PATH == database


def test_unselected_adapter_uses_the_configured_database_auth_and_lock_prefix(tmp_path):
    with patch.dict(os.environ, settings(tmp_path), clear=True):
        bus = load_bus()
        assert bus.DB_PATH == tmp_path / "default-inbox.sqlite3"
        assert bus.auth_header_path() == tmp_path / "default-auth.hdr"
        assert nw_paths.lock_path("worker") == tmp_path / "default-locks/old-worker.lock"


def test_selected_fleet_lock_ignores_the_configured_lock_directory(tmp_path):
    selected = tmp_path / "named-state"
    with patch.dict(os.environ, {**settings(tmp_path), "NW_FLEET_PROFILE_APPLIED": "sample",
                                 "NOTES_RUNTIME_DIR": str(selected)}, clear=True):
        assert nw_paths.lock_path("worker") == selected / "cache/locks/worker.lock"


def test_lock_prefix_cannot_escape_configured_directory(tmp_path):
    env = settings(tmp_path)
    config = Path(env["FLEET_ORCHESTRATOR_CONFIG"])
    value = json.loads(config.read_text())
    value["paths"]["lock_prefix"] = "../other/"
    config.write_text(json.dumps(value))
    with patch.dict(os.environ, env, clear=True):
        try:
            nw_paths.lock_path("worker")
        except ValueError as exc:
            assert "filename prefix" in str(exc)
        else:
            raise AssertionError("path traversal prefix was accepted")
