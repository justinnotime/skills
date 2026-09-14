"""Verify launcher contracts, without launching an authenticated native agent."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render-launchers.sh"


class CursorProfilesTest(unittest.TestCase):
    def test_independent_repository_configs_two_nodes_two_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime = base / "runtime"
            shutil.copytree(SCRIPT.parent, runtime / "scripts")
            subprocess.run(["git", "init", "-q", str(runtime)], check=True)
            script = runtime / "scripts/render-launchers.sh"
            for node in ("node-a", "node-b"):
                home = base / node
                home.mkdir()
                legacy = home / ".config/backup/config"
                legacy.parent.mkdir(parents=True)
                legacy.write_text("source /missing/unrelated-repo/config\n")
                command = home / "record-command"
                command.write_text(
                    '#!/usr/bin/env python3\nimport json,os,sys\nprint(json.dumps([os.environ["CURSOR_CONFIG_DIR"],sys.argv[1:],os.getcwd()]))\nsys.exit(7)\n'
                )
                command.chmod(0o700)
                for label in ("alpha", "beta"):
                    root = home / f"cursor {label}"
                    config = home / f"{label}.conf"
                    config.write_text(
                        f"CURSOR_COMMAND={shlex.quote(str(command))}\nCURSOR_PROFILES={shlex.quote(f'{label}:{root}')}\n"
                    )
                    env = os.environ | {"HOME": str(home)}
                    result = subprocess.run(
                        ["bash", str(script), "--config", str(config)],
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    launcher = home / "launchers.sh"
                    launcher.write_text(result.stdout)
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            f'source "$1" && cursor-agent-{label} "one argument"',
                            "test",
                            str(launcher),
                        ],
                        env=env,
                        cwd=home,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 7, result.stderr)
                    self.assertEqual(
                        json.loads(result.stdout),
                        [str(root), ["one argument"], str(home)],
                    )
                    self.assertFalse(root.exists())

    def test_conflicting_or_aliased_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            home.mkdir()
            runtime = base / "runtime"
            shutil.copytree(SCRIPT.parent, runtime / "scripts")
            subprocess.run(["git", "init", "-q", str(runtime)], check=True)
            script = runtime / "scripts/render-launchers.sh"
            root = home / "root"
            root.mkdir()
            (home / "redirect").symlink_to(root)
            for entries in (
                f"alpha:{root}\nbeta:{home}/redirect",
                f"alpha:{home}/.cursor",
            ):
                config = home / "config"
                config.write_text(
                    f"CURSOR_COMMAND=/bin/true\nCURSOR_PROFILES={shlex.quote(entries)}\n"
                )
                result = subprocess.run(
                    ["bash", str(script), "--config", str(config), "--check"],
                    env=os.environ | {"HOME": str(home)},
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
