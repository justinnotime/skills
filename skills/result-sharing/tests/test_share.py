import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/share.py"
spec = importlib.util.spec_from_file_location("share", SCRIPT)
share = importlib.util.module_from_spec(spec)
spec.loader.exec_module(share)


class Sharing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "index.html").write_text(
            '<h1>中文结果</h1><script src="app.js"></script>'
        )
        (self.source / "app.js").write_text("window.ready=true;")
        self.root = self.base / "hub"
        self.cfg = {
            "schema": share.SCHEMA,
            "root": str(self.root),
            "base_url": "http://localhost:8888",
            "allowed_source_roots": [str(self.source)],
        }

    def bundle(self, **options):
        args = dict(
            source=str(self.source),
            project="demo",
            title="中文标题",
            entry="index.html",
            files=["index.html", "app.js"],
            summary="A result",
        )
        args.update(options)
        return share.bundle(self.cfg, **args)

    def test_durable_immutable_idempotent_navigation(self):
        value = self.bundle()
        first = share.publish(self.cfg, value)
        self.assertEqual(first, share.publish(self.cfg, value))
        release = self.root / "projects/demo" / first["revision"]
        self.assertEqual(
            (release / "files/index.html").read_bytes(),
            (self.source / "index.html").read_bytes(),
        )
        value["summary"] = "A later result"
        second = share.publish(self.cfg, value)
        self.assertNotEqual(first["result_url"], second["result_url"])
        rows = json.loads((self.root / "catalog.json").read_text())["projects"]
        self.assertEqual(rows[0]["revision"], second["revision"])
        self.assertIn(
            first["revision"], (self.root / "projects/demo/index.html").read_text()
        )
        self.assertIn(
            second["revision"], (self.root / "projects/demo/index.html").read_text()
        )
        (self.root / "index.html").unlink()
        share.publish(self.cfg, value)
        self.assertTrue((self.root / "index.html").is_file())

    def test_selected_files_only_and_source_boundary(self):
        (self.source / "credentials.txt").write_text("synthetic-do-not-publish")
        value = self.bundle()
        self.assertEqual(set(value["files"]), {"index.html", "app.js"})
        with self.assertRaises(ValueError):
            self.bundle(source=str(self.base))
        (self.source / "linked.html").symlink_to(self.source / "index.html")
        with self.assertRaises(ValueError):
            self.bundle(entry="linked.html", files=["linked.html"])

    def test_malformed_bundle_has_no_side_effects(self):
        original = self.bundle()
        cases = []
        for name in (
            "../escape",
            "/absolute",
            ".git/config",
            "a/../b",
            "a\\b",
            "a//b",
            "x\nfile",
        ):
            bad = copy.deepcopy(original)
            bad["files"][name] = bad["files"].pop("app.js")
            cases.append(bad)
        for key, value in (
            ("project", "../escape"),
            ("entry", "missing"),
            ("conversation_url", "javascript:alert(1)"),
        ):
            bad = copy.deepcopy(original)
            bad[key] = value
            cases.append(bad)
        bad = copy.deepcopy(original)
        bad["files"]["app.js"]["sha256"] = "0" * 64
        cases.append(bad)
        for bad in cases:
            with self.subTest(case=len(str(bad))):
                with self.assertRaises(ValueError):
                    share.receive(self.cfg, bad)
                self.assertFalse(self.root.exists())

    def test_limits_and_receiver_project_authorization(self):
        value = self.bundle()
        for extra in (
            {"max_bytes": 1},
            {"max_files": 1},
            {"allowed_projects": ["another"]},
        ):
            with self.assertRaises(ValueError):
                share.receive(dict(self.cfg, **extra), value)
        self.assertFalse(self.root.exists())

    def test_metadata_is_escaped_and_files_encoded(self):
        (self.source / 'a"&.txt').write_text("sample")
        value = self.bundle(
            title="<script>evil</script>",
            files=["index.html", 'a"&.txt'],
            conversation_url="https://viewer.example/session?a=1&b=2",
        )
        result = share.publish(self.cfg, value)
        document = (
            self.root / "projects/demo" / result["revision"] / "index.html"
        ).read_text()
        self.assertNotIn("<script>evil</script>", document)
        self.assertIn("&lt;script&gt;evil", document)
        self.assertIn("a%22%26.txt", document)
        self.assertIn("a=1&amp;b=2", document)

    def test_destination_symlink_and_modified_revision_fail(self):
        self.root.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            share.publish(self.cfg, self.bundle())
        self.root.unlink()
        value = self.bundle()
        result = share.publish(self.cfg, value)
        (self.root / "projects/demo" / result["revision"] / "files/app.js").write_text(
            "changed"
        )
        with self.assertRaises(ValueError):
            share.publish(self.cfg, value)

    def test_standalone_cli_dry_run_and_receiver(self):
        isolated = self.base / "isolated"
        shutil.copytree(SCRIPT.parents[1], isolated)
        cfg = self.base / "config.json"
        cfg.write_text(json.dumps(self.cfg))
        command = [
            sys.executable,
            "-B",
            str(isolated / "scripts/share.py"),
            "--config",
            str(cfg),
        ]
        run = subprocess.run(
            command
            + [
                "publish",
                "--source",
                str(self.source),
                "--project",
                "demo",
                "--title",
                "A title",
                "--entry",
                "index.html",
                "--files",
                "index.html",
                "app.js",
                "--dry-run",
            ],
            capture_output=True,
            check=True,
        )
        self.assertFalse(json.loads(run.stdout)["published"])
        self.assertFalse(self.root.exists())
        run = subprocess.run(
            command + ["receive"],
            input=share.canonical(self.bundle()),
            capture_output=True,
            check=True,
        )
        self.assertIn("result_url", json.loads(run.stdout))

    def test_ssh_command_boundaries_and_failure(self):
        cfg = dict(
            self.cfg,
            transport={
                "kind": "ssh",
                "target": "test-hub",
                "receiver_command": [
                    "python3",
                    "/path with spaces/share.py",
                    "receive",
                ],
            },
        )
        value = self.bundle(title="literal $(do-not-execute)")
        receipt = share.publish(self.cfg, value)
        with patch.object(
            share.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, share.canonical(receipt), b""
            ),
        ) as run:
            self.assertEqual(share.publish(cfg, value), receipt)
            argv = run.call_args.args[0]
            self.assertNotIn(value["title"], " ".join(argv))
            self.assertIn("'/path with spaces/share.py'", argv[-1])
            self.assertEqual(
                json.loads(run.call_args.kwargs["input"])["title"], value["title"]
            )
        with patch.object(
            share.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, b"", b"private diagnostic"),
        ):
            with self.assertRaises(ValueError):
                share.publish(cfg, value)

    def test_concurrent_cli_publication_preserves_both_projects(self):
        cfg = self.base / "config.json"
        cfg.write_text(json.dumps(self.cfg))
        command = [sys.executable, "-B", str(SCRIPT), "--config", str(cfg), "receive"]
        processes = []
        for project in ("first", "second"):
            p = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            p.stdin.write(share.canonical(self.bundle(project=project)))
            p.stdin.close()
            processes.append(p)
        for p in processes:
            self.assertEqual(p.wait(timeout=10), 0)
            p.stdout.close()
            p.stderr.close()
        projects = {
            r["project"]
            for r in json.loads((self.root / "catalog.json").read_text())["projects"]
        }
        self.assertEqual(projects, {"first", "second"})

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(ValueError):
            share.decode('{"files":{}, "files":{}}')

    def test_file_browser_preserves_history_and_indexes_only_published_files(self):
        (self.source / "private.txt").write_text("not selected")
        nested = self.source / "reports" / "notes.md"
        nested.parent.mkdir()
        nested.write_text("# Earlier report")
        first = share.publish(
            self.cfg, self.bundle(entry="reports/notes.md", files=["reports/notes.md"])
        )
        second = share.publish(self.cfg, self.bundle(summary="New result"))
        manifests = list(self.root.glob("projects/*/*/manifest.json"))
        before = {p: p.read_bytes() for p in manifests}
        share.catalog(self.root)
        library = json.loads((self.root / "library.json").read_text())
        releases = library["projects"][0]["releases"]
        self.assertEqual(
            [r["revision"] for r in releases], [second["revision"], first["revision"]]
        )
        self.assertIn("reports/notes.md", releases[1]["files"])
        self.assertNotIn("private.txt", json.dumps(library))
        self.assertEqual(before, {p: p.read_bytes() for p in manifests})
        for name in ("library.js", "library.css"):
            self.assertEqual(
                (self.root / name).read_bytes(), (share.ASSETS / name).read_bytes()
            )
        self.assertIn('id="projects"', (self.root / "index.html").read_text())
        self.assertTrue(
            (
                self.root
                / "projects/demo"
                / first["revision"]
                / "files/reports/notes.md"
            ).exists()
        )

    def test_empty_catalog_and_unpublished_directories(self):
        self.root.mkdir()
        (self.root / "private.txt").write_text("not published")
        share.catalog(self.root)
        self.assertEqual(
            json.loads((self.root / "library.json").read_text())["projects"], []
        )
        self.assertNotIn("private.txt", (self.root / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
