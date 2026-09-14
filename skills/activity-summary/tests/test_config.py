import json

import pytest
from conftest import synthetic_config

from activity_summary import facts
from activity_summary.config import ConfigurationError, activate, home, load, rooted


def save(tmp_path, cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return path


def test_home_expansion_is_portable_and_other_variables_are_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert home("~/data") == str(tmp_path / "data")
    assert home("${HOME}/data") == str(tmp_path / "data")
    assert home("$HOME/data") == str(tmp_path / "data")
    assert home("$OTHER/data") == "$OTHER/data"


@pytest.mark.parametrize("path", ["../outside", "/absolute", "a/../b", "a//b", "a\\b"])
def test_selected_paths_must_be_relative_and_confined(tmp_path, path):
    cfg = synthetic_config(tmp_path)
    cfg["facts"]["issue_directory"] = path
    with pytest.raises(ConfigurationError):
        load(save(tmp_path, cfg))


def test_nested_symlink_is_rejected(tmp_path):
    (tmp_path / "sources").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="symlink"):
        rooted(tmp_path, "sources/secret")


def test_commit_directory_trailing_separator_preserves_legacy_selection(tmp_path):
    cfg = synthetic_config(tmp_path)
    cfg["facts"]["commit_directories"] = ["sources/", "knowledge/"]
    loaded = load(save(tmp_path, cfg))
    assert loaded["facts"]["commit_directories"] == ["sources", "knowledge"]


@pytest.mark.parametrize("directories", ["sources/old-documents", ["../outside"], [None]])
def test_document_history_directories_require_relative_path_list(tmp_path, directories):
    cfg = synthetic_config(tmp_path)
    cfg["facts"]["document_history_directories"] = directories
    with pytest.raises(ConfigurationError):
        load(save(tmp_path, cfg))


def test_private_source_labels_and_machine_filters_are_explicit(tmp_path):
    cfg = synthetic_config(tmp_path)
    cfg["facts"].update(
        {
            "anti_echo_job_name": "synthetic-summary-job",
            "anti_echo_summary_path": "summaries",
            "commit_kind_patterns": [["synthetic-lint", "automated-lint"]],
            "source_project_labels": [["sources/mail", "mail-context"]],
        }
    )
    activate(load(save(tmp_path, cfg)))
    assert facts.classify("run synthetic-lint") == "automated-lint"
    assert facts.project_of("sources/mail/a.md") == "mail-context"
    assert facts.is_machine_prompt("Scan summaries")
    assert facts.is_machine_prompt("Run synthetic-summary-job")
    assert not facts.is_machine_prompt("Please inspect the selected implementation")


def test_facts_serialization_is_exact_legacy_format():
    data = {"date": "2024-01-02", "prompt": "学习", "items": []}
    assert facts.serialize(data) == (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
