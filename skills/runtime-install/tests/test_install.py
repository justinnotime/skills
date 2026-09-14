import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from runtime_install import install


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in list(os.environ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", os.defpath)


def links(tmp_path):
    source = tmp_path / "packages/example-tool"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Synthetic skill\n")
    return {
        "schema": "runtime-install/v1",
        "kind": "skills",
        "lock": str(tmp_path / "locks/links"),
        "packages": {
            "example-tool": {"source": str(source), "required": [{"path": "SKILL.md"}]}
        },
        "destinations": [str(tmp_path / "client/skills")],
        "profiles": [],
    }


def invoke(config, tmp_path, *args):
    filename = tmp_path / "config.json"
    filename.write_text(json.dumps(config))
    return install.main(config["kind"], ["--config", str(filename), *args])


def test_links_preview_and_sources_do_not_write(tmp_path, capsys):
    config = links(tmp_path)
    assert invoke(config, tmp_path, "--dry-run") == 0
    assert json.loads(capsys.readouterr().out)[0]["action"] == "link"
    assert invoke(config, tmp_path, "--print-sources") == 0
    assert (
        json.loads(capsys.readouterr().out)["example-tool"]
        == config["packages"]["example-tool"]["source"]
    )
    assert not (tmp_path / "client").exists()
    assert not (tmp_path / "locks").exists()


def test_missing_source_prevents_all_links_and_profiles(tmp_path):
    config = links(tmp_path)
    config["packages"]["missing"] = {
        "source": str(tmp_path / "missing"),
        "required": [],
    }
    assert invoke(config, tmp_path) == 1
    assert not (tmp_path / "client").exists()
    assert not (tmp_path / "locks").exists()


def test_custom_entries_and_owned_profile_links(tmp_path):
    config = links(tmp_path)
    target = tmp_path / "client/skills/example-tool"
    target.mkdir(parents=True)
    (target / "local").write_text("keep")
    profile = tmp_path / "private profile.md"
    profile.write_text("Synthetic preferences")
    destination = tmp_path / "config/profile.md"
    destination.parent.mkdir()
    other = tmp_path / "custom.md"
    other.write_text("custom")
    destination.symlink_to(other)
    config["profiles"] = [{"source": str(profile), "destination": str(destination)}]
    assert invoke(config, tmp_path) == 0
    assert (target / "local").read_text() == "keep"
    assert destination.resolve() == other
    destination.unlink()
    destination.symlink_to(profile)
    assert invoke(config, tmp_path) == 0
    assert not os.path.isabs(os.readlink(destination))
    assert destination.resolve() == profile


def test_owned_retirement_requires_available_replacement(tmp_path):
    config = links(tmp_path)
    dest = tmp_path / "client/skills"
    dest.mkdir(parents=True)
    old = dest / "old-tool"
    old.symlink_to("/example/old-source")
    current = dest / "example-tool"
    current.mkdir()
    config["retired_links"] = [
        {
            "path": str(old),
            "replacement": str(current),
            "owned_targets": ["/example/old-source"],
        }
    ]
    assert invoke(config, tmp_path) == 0
    assert old.is_symlink()
    current.rmdir()
    assert invoke(config, tmp_path) == 0
    assert not old.is_symlink()
    assert current.resolve() == Path(config["packages"]["example-tool"]["source"])


def test_link_failure_restores_old_links_and_removes_new(tmp_path, monkeypatch):
    config = links(tmp_path)
    config["destinations"] += [
        str(tmp_path / "second/skills"),
        str(tmp_path / "third/skills"),
    ]
    first = tmp_path / "client/skills/example-tool"
    first.parent.mkdir(parents=True)
    first.symlink_to("/example/previous")
    real = install.set_link
    calls = []

    def fail_third(target, value):
        calls.append(target)
        if len(calls) == 3:
            raise OSError("synthetic disk failure")
        return real(target, value)

    monkeypatch.setattr(install, "set_link", fail_third)
    assert invoke(config, tmp_path) == 1
    assert os.readlink(first) == "/example/previous"
    assert not (tmp_path / "second").exists()
    assert not (tmp_path / "third").exists()


def cron(tmp_path):
    (tmp_path / "crontab").write_text("SHELL=/bin/sh\n7 * * * * preserve-me\n")
    fake = tmp_path / "fake-crontab.py"
    fake.write_text("""import sys
from pathlib import Path
root = Path(__file__).parent
state = root / 'crontab'
if sys.argv[1] == '-l':
    if (root / 'read-error').exists():
        print('synthetic diagnostic', file=sys.stderr); sys.exit(2)
    if not state.exists():
        print('no crontab for synthetic-user', file=sys.stderr); sys.exit(1)
    sys.stdout.buffer.write(state.read_bytes())
elif sys.argv[1] == '-r':
    state.unlink(missing_ok=True)
else:
    content = Path(sys.argv[1]).read_bytes()
    if (root / 'corrupt-once').exists():
        (root / 'corrupt-once').unlink()
        content += b'corrupt\\n'
    state.write_bytes(content)
""")
    return {
        "schema": "runtime-install/v1",
        "kind": "cron",
        "lock": str(tmp_path / "locks/cron"),
        "backup_directory": str(tmp_path / "backups"),
        "directories": [str(tmp_path / "logs")],
        "markers": ["# BEGIN synthetic jobs", "# END synthetic jobs"],
        "lines": ["15 * * * * /example/bin/run --config /example/job.json"],
        "crontab_command": [sys.executable, str(fake)],
    }


def test_cron_preview_idempotence_unrelated_lines_and_backup(tmp_path, capsys):
    config = cron(tmp_path)
    original = (tmp_path / "crontab").read_bytes()
    assert invoke(config, tmp_path, "--dry-run") == 0
    preview = capsys.readouterr().out.encode()
    assert (tmp_path / "crontab").read_bytes() == original
    assert not (tmp_path / "locks").exists() and not (tmp_path / "backups").exists()
    assert invoke(config, tmp_path) == 0
    assert (tmp_path / "crontab").read_bytes() == preview
    assert next((tmp_path / "backups").iterdir()).read_bytes() == original
    assert all(
        p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / "backups").iterdir()
    )
    assert invoke(config, tmp_path) == 0
    assert (tmp_path / "crontab").read_bytes() == preview


def test_command_absorption_distinguishes_config_repo_and_spaces(tmp_path):
    config = cron(tmp_path)
    config["remove_commands"] = [
        ["/example space/bin/run", "--config", "/example space/selected.json"],
        ["/example/repo/old.sh"],
    ]
    selected = (
        "1 * * * * '/example space/bin/run' --config '/example space/selected.json'\n"
    )
    others = "2 * * * * '/example space/bin/run' --config '/example space/other.json'\n3 * * * * /example/other/old.sh\n# /example/repo/old.sh is a comment\n"
    result = install.cron_text(selected + others, config)
    assert selected not in result and result.startswith(others)


@pytest.mark.parametrize(
    "body",
    [
        "# END synthetic jobs\n# BEGIN synthetic jobs\n",
        "# BEGIN synthetic jobs\n",
        "# BEGIN synthetic jobs\n# BEGIN synthetic jobs\n# END synthetic jobs\n",
        "# BEGIN synthetic jobs\n# BEGIN other\n# END other\n# END synthetic jobs\n",
    ],
)
def test_bad_markers_do_not_call_prerequisites_or_write(tmp_path, body):
    config = cron(tmp_path)
    state = tmp_path / "crontab"
    state.write_text(body)
    config["before_apply"] = [
        {"argv": [sys.executable, "-c", 'raise RuntimeError("must not run")']}
    ]
    assert invoke(config, tmp_path) == 1
    assert state.read_text() == body and not (tmp_path / "backups").exists()


def test_checks_and_prerequisite_failure_keep_crontab(tmp_path):
    config = cron(tmp_path)
    original = (tmp_path / "crontab").read_bytes()
    failure = {"argv": [sys.executable, "-c", "raise SystemExit(1)"]}
    for phase in ["checks", "before_apply"]:
        config[phase] = [failure]
        assert invoke(config, tmp_path) == 1
        assert (tmp_path / "crontab").read_bytes() == original
        assert not (tmp_path / "backups").exists()
        config.pop(phase)


@pytest.mark.parametrize("absent", [False, True])
def test_verification_failure_restores_bytes_or_absence(tmp_path, absent):
    config = cron(tmp_path)
    state = tmp_path / "crontab"
    original = state.read_bytes()
    if absent:
        state.unlink()
    (tmp_path / "corrupt-once").touch()
    assert invoke(config, tmp_path) == 1
    assert not state.exists() if absent else state.read_bytes() == original


def test_cron_read_failure_never_installs_empty_replacement(tmp_path):
    config = cron(tmp_path)
    old = (tmp_path / "crontab").read_bytes()
    (tmp_path / "read-error").touch()
    assert invoke(config, tmp_path) == 1
    assert (tmp_path / "crontab").read_bytes() == old
    assert not (tmp_path / "backups").exists()


def test_named_block_preserves_other_fleets(tmp_path):
    config = cron(tmp_path)
    other = "# BEGIN another fleet\n5 * * * * /example/other\n# END another fleet\n"
    result = install.cron_text(
        other + "# BEGIN synthetic jobs\nold\n# END synthetic jobs\n", config
    )
    assert result.startswith(other) and result.count(config["markers"][0]) == 1
    assert "\nold\n" not in result


def test_concurrent_installers_reread_under_same_lock(tmp_path):
    first = cron(tmp_path)
    waiting = tmp_path / "waiting"
    release = tmp_path / "release"
    helper = tmp_path / "hold.py"
    helper.write_text(
        'import sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text("ready")\nwhile not Path(sys.argv[2]).exists(): time.sleep(0.01)\n'
    )
    first["before_apply"] = [
        {"argv": [sys.executable, str(helper), str(waiting), str(release)]}
    ]
    second = dict(first, markers=["# BEGIN second", "# END second"], before_apply=[])
    one = tmp_path / "one.json"
    one.write_text(json.dumps(first))
    two = tmp_path / "two.json"
    two.write_text(json.dumps(second))
    entry = Path(__file__).parents[1] / "scripts/cron"
    env = {**os.environ, "RUNTIME_INSTALL_PYTHON": sys.executable}
    a = subprocess.Popen(
        [str(entry), "--config", str(one)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    b = None
    try:
        deadline = time.monotonic() + 5
        while not waiting.exists() and a.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert waiting.exists()
        b = subprocess.Popen(
            [str(entry), "--config", str(two)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        release.touch()
        assert a.communicate(timeout=10)[1] == b""
        assert b.communicate(timeout=10)[1] == b""
        assert a.returncode == b.returncode == 0
        final = (tmp_path / "crontab").read_text()
        assert first["markers"][0] in final and second["markers"][0] in final
    finally:
        release.touch()
        for process in (a, b):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()


@pytest.mark.parametrize(
    "field,value", [("lock", "relative.lock"), ("destinations", ["relative"])]
)
def test_invalid_paths_fail_without_installation(tmp_path, field, value):
    config = links(tmp_path)
    config[field] = value
    assert invoke(config, tmp_path) == 1
    assert not (tmp_path / "client").exists()


def test_parent_file_preflight_does_not_partially_install(tmp_path):
    config = links(tmp_path)
    (tmp_path / "occupied").write_text("do not replace")
    config["destinations"].append(str(tmp_path / "occupied/skills"))
    assert invoke(config, tmp_path) == 1
    assert not (tmp_path / "client").exists()
    assert (tmp_path / "occupied").read_text() == "do not replace"


def test_preflight_failure_does_not_print_external_diagnostics(tmp_path, capsys):
    config = cron(tmp_path)
    config["checks"] = [
        {
            "argv": [
                sys.executable,
                "-c",
                'import sys;print("synthetic-private-body",file=sys.stderr);raise SystemExit(1)',
            ]
        }
    ]
    assert invoke(config, tmp_path) == 1
    output = capsys.readouterr()
    assert "synthetic-private-body" not in output.err + output.out
    assert not (tmp_path / "locks").exists()


def test_exact_unmarked_trigger_is_adopted_without_removing_other_consumers():
    line = "* * * * /example/data-pipeline/scripts/run"
    other = line + " --config /another/repository/node.json"
    config = {"markers": ["# BEGIN sample", "# END sample"], "lines": [line]}
    result = install.cron_text(line + "\n" + other + "\n", config)
    assert result.splitlines().count(line) == 1
    assert other + "\n" in result
    assert install.cron_text(result, config) == result
