import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from data_pipeline import Invalid, doctor, due, execute, load


def make_config(tmp_path):
    repo = tmp_path / "consumer repo"
    repo.mkdir()
    (repo / "config").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (repo / "config/source.json").write_text(json.dumps({"partitions": ["alpha"]}))
    writer = repo / "writer.py"
    writer.write_text(
        "import os, pathlib\np=pathlib.Path(os.environ['RESULT'])\np.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
    )
    catalog = {
        "schema": "data-pipeline/v1",
        "repository": "..",
        "state_directory": "$HOME/state",
        "environment": {"PATH": "/usr/bin:/bin"},
        "nodes": {"alpha": {"timezone": "UTC"}, "beta": {"timezone": "UTC"}},
        "resources": {
            "settings": {
                "kind": "config",
                "scope": "repository",
                "paths": ["config/source.json"],
            },
            "output": {
                "kind": "data",
                "scope": "repository",
                "paths": ["output"],
                "partition_contract": "synthetic configured partition writer",
            },
        },
        "jobs": [
            {
                "id": "produce",
                "node": "alpha",
                "schedule": "* * * * *",
                "argv": [sys.executable, "$REPO/writer.py"],
                "environment": {"RESULT": "$REPO/output"},
                "inputs": [],
                "dependencies": ["settings"],
                "outputs": [
                    {
                        "resource": "output",
                        "partitions_from": {
                            "file": "config/source.json",
                            "pointer": "/partitions",
                        },
                    }
                ],
                "timeout_seconds": 10,
                "log": "$HOME/job.log",
            }
        ],
    }
    selector = repo / "config/alpha.json"
    selector.write_text(
        json.dumps(
            {
                "schema": "data-pipeline-node/v1",
                "catalog": "pipelines.json",
                "node": "alpha",
            }
        )
    )

    def save():
        (repo / "config/pipelines.json").write_text(json.dumps(catalog))
        return load(selector, home)

    save()
    return repo, home, selector, catalog, save


def test_missing_outputs_are_created_and_slots_do_not_repeat(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    job = save()["jobs"][0]
    assert doctor(job) == []
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert execute(job, at)["status"] == "ok"
    assert execute(job, at)["status"] == "already-attempted"
    assert (repo / "output").read_text() == "x"
    assert execute(job, at.replace(minute=1))["status"] == "ok"
    assert (repo / "output").read_text() == "xx"
    assert json.loads((home / "state/produce.json").read_text())["returncode"] == 0


def test_concurrent_ticks_and_long_running_job(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    (repo / "writer.py").write_text(
        "import pathlib,time,os\ntime.sleep(.3)\npathlib.Path(os.environ['RESULT']).write_text('once')\n"
    )
    job = save()["jobs"][0]
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: execute(job, at), range(2)))
    assert sorted(r["status"] for r in results) == ["busy", "ok"]
    assert (repo / "output").read_text() == "once"


def test_job_failure_is_recorded_and_next_schedule_retries(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    (repo / "writer.py").write_text("raise SystemExit(7)\n")
    job = save()["jobs"][0]
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = execute(job, at)
    assert (result["status"], result["returncode"]) == ("failed", 7)
    assert execute(job, at)["status"] == "already-attempted"
    (repo / "writer.py").write_text("pass\n")
    assert execute(job, at.replace(minute=1))["status"] == "ok"


@pytest.mark.parametrize("failure_at", [0, 1, 2])
def test_native_command_failure_stops_all_later_commands(tmp_path, failure_at):
    repo, home, selector, catalog, save = make_config(tmp_path)
    job_config = catalog["jobs"][0]
    del job_config["argv"]
    job_config["commands"] = [
        [sys.executable, "-c", f"from pathlib import Path; Path('step-{i}').touch(); raise SystemExit({17 if i == failure_at else 0})"]
        for i in range(3)
    ]
    job = save()["jobs"][0]
    result = execute(job, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert (result["status"], result["returncode"]) == ("failed", 17)
    assert result["command_index"] == failure_at + 1
    assert result["completed_commands"] == failure_at
    assert {p.name for p in repo.glob("step-*")} == {f"step-{i}" for i in range(failure_at + 1)}
    assert json.loads((home / "state/produce.json").read_text()) == result


def test_commands_share_environment_order_and_attempt_lock(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    config = catalog["jobs"][0]
    first = config.pop("argv")
    config["commands"] = [first, first]
    job = save()["jobs"][0]
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = execute(job, at)
    assert result["status"] == "ok" and result["completed_commands"] == 2
    assert (repo / "output").read_text() == "xx"
    assert execute(job, at)["status"] == "already-attempted"
    assert (repo / "output").read_text() == "xx"


def test_all_command_executables_are_checked_before_first_write(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    config = catalog["jobs"][0]
    config["commands"] = [config.pop("argv"), ["/does-not-exist/fetch"]]
    result = execute(save()["jobs"][0], datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["status"] == "failed"
    assert not (repo / "output").exists()


def test_command_sequence_shares_one_total_timeout(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    config = catalog["jobs"][0]
    del config["argv"]
    config["timeout_seconds"] = .4
    config["commands"] = [
        [sys.executable, "-c", "import time; time.sleep(.25)"],
        [sys.executable, "-c", "import time; time.sleep(.25)"],
        [sys.executable, "-c", "from pathlib import Path; Path('published').touch()"],
    ]
    result = execute(save()["jobs"][0], datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["status"] == "failed" and result["error"] == "timeout"
    assert result["completed_commands"] == 1
    assert "returncode" not in result
    assert not (repo / "published").exists()


@pytest.mark.parametrize("commands", [[], "false", [[]], ["/usr/bin/true"], [["relative-command"]]])
def test_invalid_command_sequences_are_rejected(tmp_path, commands):
    repo, home, selector, catalog, save = make_config(tmp_path)
    config = catalog["jobs"][0]
    del config["argv"]
    config["commands"] = commands
    with pytest.raises(Invalid):
        save()


def test_argv_and_commands_cannot_compete(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    catalog["jobs"][0]["commands"] = [[sys.executable, "-c", "pass"]]
    with pytest.raises(Invalid, match="exactly one"):
        save()


def test_timeout_stops_command_and_releases_lock(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    (repo / "writer.py").write_text("import time\ntime.sleep(60)\n")
    catalog["jobs"][0]["timeout_seconds"] = 0.1
    job = save()["jobs"][0]
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = execute(job, at)
    assert result["error"] == "timeout"
    (repo / "writer.py").write_text("pass\n")
    assert execute(job, at.replace(minute=1))["status"] == "ok"


def test_missing_input_blocks_writer(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    catalog["resources"]["source"] = {
        "kind": "data",
        "scope": "node",
        "paths": ["$HOME/source"],
    }
    catalog["jobs"][0]["inputs"] = ["source"]
    job = save()["jobs"][0]
    result = execute(job, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result["status"] == "failed"
    assert not (repo / "output").exists()
    assert "source: missing" in result["errors"][0]


def test_conflicting_nodes_are_rejected_from_actual_manifest(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    second = json.loads(json.dumps(catalog["jobs"][0]))
    second.update(id="second", node="beta")
    catalog["jobs"].append(second)
    with pytest.raises(Invalid, match="overlapping output ownership"):
        save()
    second["outputs"][0] = {"resource": "output", "partitions": ["beta"]}
    assert len(save()["jobs"]) == 2
    (repo / "config/source.json").write_text(json.dumps({"partitions": ["beta"]}))
    with pytest.raises(Invalid, match="overlapping output ownership"):
        save()


def test_parent_child_claims_cannot_hide_behind_different_resource_names(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    catalog["resources"]["child"] = {
        "kind": "data",
        "scope": "repository",
        "paths": ["output/child"],
    }
    second = json.loads(json.dumps(catalog["jobs"][0]))
    second.update(id="second", node="beta", outputs=[{"resource": "child"}])
    catalog["jobs"].append(second)
    with pytest.raises(Invalid, match="overlapping output ownership"):
        save()


def test_missing_and_outside_repo_config_are_rejected(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    catalog["resources"]["settings"]["paths"] = ["$HOME/copied-config.json"]
    (home / "copied-config.json").write_text("{}")
    with pytest.raises(Invalid, match="inside the repository"):
        save()
    catalog["resources"]["settings"]["paths"] = ["config/missing.json"]
    with pytest.raises(Invalid, match="missing repository configuration"):
        save()


def test_transitive_dependencies_and_cycles(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    catalog["resources"]["nested"] = {
        "kind": "config",
        "scope": "repository",
        "paths": ["config/source.json"],
    }
    catalog["resources"]["settings"]["requires"] = ["nested"]
    assert "nested" in save()["jobs"][0]["resources"]
    catalog["resources"]["nested"]["requires"] = ["settings"]
    with pytest.raises(Invalid, match="dependency cycle"):
        save()


def test_default_zero_argument_entry_is_isolated_from_ambient_input(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    (repo / "writer.py").write_text(
        "import os,pathlib,sys\nassert 'UNDECLARED_INPUT' not in os.environ\nassert os.environ['RESULT'].endswith('/output')\nassert sys.argv[1] == 'literal $(touch bad) argument'\npathlib.Path(os.environ['RESULT']).write_text('ok')\n"
    )
    catalog["jobs"][0]["argv"].append("literal $$(touch bad) argument")
    save()
    installed = home / ".config/data-pipeline"
    installed.mkdir(parents=True)
    (installed / "config.json").symlink_to(selector)
    runtime = tmp_path / "isolated-skill"
    package = Path(__file__).resolve().parents[1]
    shutil.copytree(package / "src", runtime / "src")
    shutil.copytree(package / "scripts", runtime / "scripts")
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "UNDECLARED_INPUT": "wrong",
        "RESULT": "/wrong",
    }
    result = subprocess.run(
        [str(runtime / "scripts/run")], env=env, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert (repo / "output").read_text() == "ok"
    assert not (repo / "bad").exists()


def test_plan_and_doctor_do_not_create_logs_state_or_run_jobs(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    entry = Path(__file__).resolve().parents[1] / "scripts/run"
    for option in ["--plan", "--doctor"]:
        result = subprocess.run(
            [str(entry), "--config", str(selector), option],
            env={"HOME": str(home)},
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        assert not (home / "state").exists()
        assert not (home / "job.log").exists()
        assert not (repo / "output").exists()
    result = subprocess.run(
        [str(entry), "--config", str(selector), "--run", "other-node-job"],
        env={"HOME": str(home)},
        capture_output=True,
    )
    assert result.returncode == 2
    assert not (home / "state").exists()


@pytest.mark.parametrize(
    "expression,at,expected",
    [
        ("4-59/5 * * * *", "2026-01-01T00:04:00+00:00", True),
        ("4-59/5 * * * *", "2026-01-01T00:05:00+00:00", False),
        ("50 1-23 * * 5", "2026-09-11T01:50:00+00:00", True),
        ("50 1-23 * * 5", "2026-09-11T00:50:00+00:00", False),
        ("0 0 1 * 5", "2026-09-11T00:00:00+00:00", True),
        ("0 0 1 * 5", "2026-09-01T00:00:00+00:00", True),
        ("0 0 * * 7", "2026-09-13T00:00:00+00:00", True),
        ("0 0 */2 * 5", "2026-09-11T00:00:00+00:00", True),
    ],
)
def test_numeric_cron_compatibility(expression, at, expected):
    assert due(expression, datetime.fromisoformat(at)) is expected


def test_timeout_also_stops_descendant_after_wrapper_exits(tmp_path):
    repo, home, selector, catalog, save = make_config(tmp_path)
    descendant = repo / "descendant.py"
    descendant.write_text("import signal,time,pathlib\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(1)\npathlib.Path('escaped-timeout').touch()\n")
    (repo / "writer.py").write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable, 'descendant.py'])\ntime.sleep(60)\n")
    catalog["jobs"][0]["timeout_seconds"] = .3
    job = save()["jobs"][0]
    assert execute(job, datetime(2026, 1, 1, tzinfo=timezone.utc))["error"] == "timeout"
    import time
    time.sleep(1)
    assert not (repo / "escaped-timeout").exists()


@pytest.fixture
def profiles(tmp_path):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    repo, home, selector, catalog, save = make_config(first)
    other, _, other_selector, other_catalog, other_save = make_config(second)
    other_catalog['state_directory'] = '$HOME/other-state'
    other_catalog['jobs'][0]['log'] = '$HOME/other-job.log'
    other_save()
    installed = home / '.config/data-pipeline'
    (installed / 'profiles').mkdir(parents=True)
    (installed / 'config.json').symlink_to(selector)
    (installed / 'profiles/alpha.json').symlink_to(selector)
    (installed / 'profiles/beta.json').symlink_to(other_selector)
    runtime = tmp_path / 'isolated-skill'
    package = Path(__file__).resolve().parents[1]
    shutil.copytree(package / 'src', runtime / 'src')
    shutil.copytree(package / 'scripts', runtime / 'scripts')

    def run(*args, environment=None):
        return subprocess.run(
            [str(runtime / 'scripts/run'), *args],
            env={'HOME': str(home), 'PATH': '/usr/bin:/bin', **(environment or {})},
            text=True, capture_output=True,
        )

    return repo, other, home, selector, installed, run


def test_existing_default_ignores_named_profiles_and_their_failures(profiles):
    repo, other, home, selector, installed, run = profiles
    (installed / 'profiles/beta.json').unlink()
    (installed / 'profiles/beta.json').write_text('invalid JSON')
    result = run()
    assert result.returncode == 0, result.stderr
    assert (repo / 'output').read_text() == 'x'
    assert not (other / 'output').exists()
    assert not (home / 'other-state').exists()


@pytest.mark.parametrize('arguments', [[], ['--run', 'produce']])
def test_named_profile_runs_only_its_repository(profiles, arguments):
    repo, other, home, selector, installed, run = profiles
    result = run('--profile', 'beta', *arguments)
    assert result.returncode == 0, result.stderr
    assert (other / 'output').read_text() == 'x'
    assert not (repo / 'output').exists()
    assert not (home / 'state').exists()
    record = json.loads((home / 'other-state/produce.json').read_text())
    assert record['status'] == 'ok'
    assert (home / 'other-job.log').exists()


@pytest.mark.parametrize('mode', ['--plan', '--doctor'])
def test_profile_inspection_and_explicit_config_remain_read_only(profiles, mode):
    repo, other, home, selector, installed, run = profiles
    results = [run(mode), run('--profile', 'alpha', mode),
               run('--config', str(selector), mode)]
    for result in results:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == json.loads(results[0].stdout)
    assert not (repo / 'output').exists()
    assert not (other / 'output').exists()
    assert not (home / 'state').exists()
    assert not (home / 'job.log').exists()


def test_xdg_profile_selection_does_not_change_default_home_selection(profiles, tmp_path):
    repo, other, home, selector, installed, run = profiles
    xdg = tmp_path / 'alternate config'
    (xdg / 'data-pipeline/profiles').mkdir(parents=True)
    target = (installed / 'profiles/beta.json').resolve()
    (xdg / 'data-pipeline/config.json').symlink_to(target)
    (xdg / 'data-pipeline/profiles/alpha.json').symlink_to(target)
    for arguments in ([], ['--profile', 'alpha']):
        result = run(*arguments, '--plan', environment={'XDG_CONFIG_HOME': str(xdg)})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['repository'] == str(other)
    result = run('--profile', 'alpha', '--plan')
    assert json.loads(result.stdout)['repository'] == str(repo)
    result = run('--config', str(selector), '--plan',
                 environment={'XDG_CONFIG_HOME': str(xdg)})
    assert json.loads(result.stdout)['repository'] == str(repo)


@pytest.mark.parametrize('name', ['missing', '', '../config', '/tmp/selector',
                                 'a/b', '.', '..', 'a\\b', 'two words'])
def test_bad_or_missing_profile_never_falls_back_to_default(profiles, name):
    repo, other, home, selector, installed, run = profiles
    result = run('--profile', name)
    assert result.returncode == 2, result.stderr
    assert not (repo / 'output').exists()
    assert not (other / 'output').exists()
    assert not (home / 'state').exists()
    assert not (home / 'other-state').exists()


def test_selection_options_are_mutually_exclusive(profiles):
    repo, other, home, selector, installed, run = profiles
    result = run('--profile', 'beta', '--config', str(selector))
    assert result.returncode == 2
    assert not (repo / 'output').exists()
    assert not (other / 'output').exists()


def test_missing_default_does_not_select_the_only_named_profile(profiles):
    repo, other, home, selector, installed, run = profiles
    (installed / 'config.json').unlink()
    (installed / 'profiles/beta.json').unlink()
    assert run().returncode == 2
    assert not (repo / 'output').exists()
    assert run('--profile', 'alpha').returncode == 0
    assert (repo / 'output').read_text() == 'x'


def test_named_profile_cannot_replace_repository_selector_with_external_copy(profiles):
    repo, other, home, selector, installed, run = profiles
    catalog = json.loads(selector.read_text())
    catalog['catalog'] = str(repo / 'config/pipelines.json')
    path = installed / 'profiles/alpha.json'
    path.unlink()
    path.write_text(json.dumps(catalog))
    result = run('--profile', 'alpha')
    assert result.returncode == 2
    assert 'inside the repository' in result.stderr
    assert not (repo / 'output').exists()


def test_default_and_explicit_profile_share_existing_attempts_and_locks(profiles, monkeypatch):
    import data_pipeline

    repo, other, home, selector, installed, run = profiles

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.setattr(data_pipeline, 'datetime', Clock)
    assert data_pipeline.main([]) == 0
    record = home / 'state/produce.json'
    original = record.read_bytes()
    for arguments in (['--profile', 'alpha'], ['--config', str(selector)]):
        assert data_pipeline.main(arguments) == 0
        assert record.read_bytes() == original
        assert (repo / 'output').read_text() == 'x'
    # Explicit runs bypass the minute check, but must still use the original lock.
    with data_pipeline.locked(home / 'state/produce.lock'):
        assert data_pipeline.main(['--profile', 'alpha', '--run', 'produce']) == 0
        assert record.read_bytes() == original
        assert (repo / 'output').read_text() == 'x'
    assert data_pipeline.main(['--profile', 'beta']) == 0
    assert (other / 'output').read_text() == 'x'
    assert (home / 'other-state/produce.json').exists()
