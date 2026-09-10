from pathlib import Path
import os
import subprocess
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def install(target, *args):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / "install"),
                           "--bin-dir", str(target), *args],
                          text=True, capture_output=True)


def test_conflicting_binary_is_preserved_before_any_command_is_written(tmp_path):
    target = tmp_path / "bin"
    target.mkdir()
    original = b"\x7fELF\xffforeign"
    (target / "tview").write_bytes(original)
    result = install(target)
    assert result.returncode != 0
    assert "--replace" in result.stderr
    assert (target / "tview").read_bytes() == original
    assert not (target / "orc").exists()


def test_tview_launcher_keeps_package_context_and_is_idempotent(tmp_path):
    target = tmp_path / "commands with spaces"
    result = install(target, "--command", "tview")
    assert result.returncode == 0, result.stderr
    env = {key: value for key, value in os.environ.items()
           if key not in {"FLEET_ORCHESTRATOR_ROOT", "TVIEW_FLEET_PROFILE"}}
    run = subprocess.run([str(target / "tview"), "--help"], env=env,
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert "Usage: tview" in run.stdout
    assert install(target, "--command", "tview").stdout == ""
    assert len(list(target.iterdir())) == 1


def test_explicit_replace_preserves_dangling_symlink(tmp_path):
    command = tmp_path / "tview"
    command.symlink_to("old-missing-entry")
    result = install(tmp_path, "--command", "tview", "--replace")
    assert result.returncode == 0, result.stderr
    backups = list(tmp_path.glob("tview.before-fleet-install-*"))
    assert len(backups) == 1
    assert os.readlink(backups[0]) == "old-missing-entry"
    assert not command.is_symlink()
