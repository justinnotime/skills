import json

import pytest

from google_docs_authority.config import load


def fixture(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    value = {
        "schema": "google-docs-authority/v1",
        "read_token_file": "read.json",
        "mirror": {
            "repository_root": "repository",
            "output_directory": "archive",
            "source_list": "selected.yaml",
            "discovered_list": "discovered.yaml",
            "state_file": "progress.json",
            "cache_directory": "cache",
            "cache_link": ".cache",
            "redact_command": ["redact", "--tier", "@tiers@"],
        },
    }
    path = tmp_path / "config.json"
    return path, value


def write(path, value):
    path.write_text(json.dumps(value))
    return path


def test_transaction_override_changes_repository_inputs_but_not_account_or_progress(
    tmp_path,
):
    path, value = fixture(tmp_path)
    transaction = tmp_path / "transaction"
    transaction.mkdir()
    cfg = load(write(path, value), transaction)
    assert cfg["mirror"]["source_list"] == transaction / "selected.yaml"
    assert cfg["mirror"]["output_directory"] == transaction / "archive"
    assert cfg["mirror"]["discovered_list"] == transaction / "discovered.yaml"
    assert cfg["read_token_file"] == tmp_path / "read.json"
    assert cfg["mirror"]["state_file"] == tmp_path / "progress.json"
    assert cfg["mirror"]["cache_directory"] == tmp_path / "cache"


def test_existing_legacy_cache_link_preserves_its_location(tmp_path):
    path, value = fixture(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    link = tmp_path / "repository/.cache"
    link.symlink_to(cache, target_is_directory=True)
    cfg = load(write(path, value))
    assert cfg["mirror"]["cache_link"] == link
    assert cfg["mirror"]["cache_link"] != cache


@pytest.mark.parametrize(
    "field", ["output_directory", "source_list", "discovered_list", "cache_link"]
)
def test_paths_cannot_escape_transaction_root(tmp_path, field):
    path, value = fixture(tmp_path)
    value["mirror"][field] = "../outside"
    with pytest.raises(ValueError, match="outside-repository"):
        load(write(path, value))


@pytest.mark.parametrize("field", ["state_file", "cache_directory", "status_file"])
def test_external_runtime_files_cannot_be_placed_inside_archive_repository(
    tmp_path, field
):
    path, value = fixture(tmp_path)
    value["mirror"][field] = "repository/progress"
    with pytest.raises(ValueError, match="inside-repository"):
        load(write(path, value))


def test_state_cannot_overwrite_token(tmp_path):
    path, value = fixture(tmp_path)
    value["mirror"]["state_file"] = value["read_token_file"]
    with pytest.raises(ValueError, match="distinct"):
        load(write(path, value))


@pytest.mark.parametrize(
    "target", ["config.json", "read.json", "progress.json", "cache"]
)
def test_status_cannot_overwrite_configuration_credential_or_progress(tmp_path, target):
    path, value = fixture(tmp_path)
    value["mirror"]["status_file"] = target
    with pytest.raises(ValueError, match="distinct"):
        load(write(path, value))


def test_status_is_external_and_not_rebased_with_transaction(tmp_path):
    path, value = fixture(tmp_path)
    value["mirror"]["status_file"] = "status.json"
    transaction = tmp_path / "transaction"
    transaction.mkdir()
    cfg = load(write(path, value), transaction)
    assert cfg["mirror"]["status_file"] == tmp_path / "status.json"


def test_absent_redactor_needs_explicit_policy_choice(tmp_path):
    path, value = fixture(tmp_path)
    del value["mirror"]["redact_command"]
    with pytest.raises(ValueError, match="command-invalid"):
        load(write(path, value))
    value["mirror"]["redact_enabled"] = False
    assert load(write(path, value))["mirror"]["redact_enabled"] is False


def test_home_is_expanded_in_external_command_without_shell_evaluation(
    tmp_path, monkeypatch
):
    path, value = fixture(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    value["mirror"]["redact_command"] = [
        "${HOME}/bin/redact",
        "@tiers@",
        "$(unchanged)",
    ]
    cfg = load(write(path, value))
    assert cfg["mirror"]["redact_command"] == [
        str(tmp_path / "bin/redact"),
        "@tiers@",
        "$(unchanged)",
    ]
    value["mirror"]["redact_command"] = ["${UNSET_GDOCS_TEST}/redact"]
    monkeypatch.delenv("UNSET_GDOCS_TEST", raising=False)
    with pytest.raises(ValueError, match="unresolved"):
        load(write(path, value))


def test_read_only_config_does_not_require_write_credential(tmp_path):
    path = tmp_path / "config.json"
    cfg = load(
        write(
            path, {"schema": "google-docs-authority/v1", "read_token_file": "read.json"}
        )
    )
    assert cfg["read_token_file"] == tmp_path / "read.json"
    assert "write_token_file" not in cfg
