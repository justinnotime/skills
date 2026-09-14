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
