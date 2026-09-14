"""Source allowlists select input before traversal, independently per root."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from session_test_support import manifest_data, write_manifest
from agent_skills.sessions.api import doctor, run
from agent_skills.sessions.manifest import ManifestError, load_manifest
from agent_skills.sessions.pipeline import PipelineError, evaluate_pipeline
from agent_skills.sessions import sources


def transcript(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "role": "user",
                "message": {
                    "content": f"<timestamp>2026-02-03T04:05:06+00:00</timestamp><user_query>{text}</user_query>"
                },
            }
        )
        + "\n"
    )


class DiscoveryDirectoriesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "output").mkdir()
        self.input = self.root / "projects"
        transcript(
            self.input / "selected/agent-transcripts/session.jsonl", "selected request"
        )
        self.data = manifest_data(
            self.input,
            self.root / "output",
            harness="cursor",
            patterns=["**/agent-transcripts/**/*.jsonl"],
        )
        self.data["sources"][0]["root_policy"].pop("forbidden_components")
        self.data["sources"][0]["discovery"]["directories"] = ["selected"]

    def load(self, data=None):
        return load_manifest(
            write_manifest(
                self.root / "manifest.json", self.data if data is None else data
            ),
            environ={},
        )

    def test_future_unselected_files_are_not_traversed_or_published(self):
        before = evaluate_pipeline(self.load())
        transcript(
            self.input / "unselected/agent-transcripts/session.jsonl",
            "UNSELECTED_PRIVATE",
        )
        real_walk = sources.os.walk
        walked = []

        def walk(path, **kwargs):
            walked.append(Path(path))
            self.assertEqual(Path(path), self.input / "selected")
            return real_walk(path, **kwargs)

        with patch.object(sources.os, "walk", side_effect=walk):
            after = evaluate_pipeline(self.load())
        self.assertTrue(walked)
        self.assertEqual(after[0].sessions, before[0].sessions)
        self.assertEqual(after[2].writes, before[2].writes)
        self.assertEqual(len(after[0].sessions), 1)

    def test_narrowing_does_not_rename_source_or_output_identity(self):
        broad = deepcopy(self.data)
        broad["sources"][0]["discovery"].pop("directories")
        before = evaluate_pipeline(self.load(broad))
        after = evaluate_pipeline(self.load())
        self.assertEqual(before[0].sessions, after[0].sessions)
        self.assertEqual(before[2].writes, after[2].writes)

    def test_missing_selection_fails_even_when_empty_is_allowed(self):
        self.data["sources"][0]["allow_empty"] = True
        self.data["sources"][0]["discovery"]["directories"] = ["missing"]
        self.load()
        self.assertEqual(
            doctor(self.root / "manifest.json", environ={})["status"], "failed"
        )
        with self.assertRaises(PipelineError):
            run(self.root / "manifest.json", dry_run=True, environ={})
        self.assertEqual(list((self.root / "output").iterdir()), [])

    def test_symlink_into_unselected_sibling_fails(self):
        transcript(
            self.input / "unselected/agent-transcripts/private.jsonl",
            "UNSELECTED_PRIVATE",
        )
        (self.input / "selected/agent-transcripts/link.jsonl").symlink_to(
            self.input / "unselected/agent-transcripts/private.jsonl"
        )
        self.load()
        with self.assertRaises(PipelineError):
            run(self.root / "manifest.json", dry_run=True, environ={})

    def test_two_nodes_two_profiles_preserve_independent_sessions(self):
        data = deepcopy(self.data)
        data["sources"] = []
        for node in ("node-a", "node-b"):
            for profile in ("alpha", "beta"):
                root = self.root / node / profile / "projects"
                transcript(
                    root / "selected/agent-transcripts/same-native-id.jsonl",
                    f"{node} {profile} selected request",
                )
                transcript(
                    root / "unselected/agent-transcripts/future.jsonl",
                    "UNSELECTED_PRIVATE",
                )
                entry = deepcopy(self.data["sources"][0])
                entry["id"] = f"{node}-{profile}"
                # Logical origin distinguishes independent native ID namespaces;
                # a replicated copy would retain this origin, not use its host.
                entry["output_node"] = f"{node}-{profile}"
                entry["path"]["value"] = str(root)
                entry["root_policy"]["allowed_lexical_roots"] = [str(root)]
                entry["root_policy"]["allowed_resolved_roots"] = [str(root)]
                data["sources"].append(entry)
        snapshot, _, plan, _, _ = evaluate_pipeline(self.load(data))
        self.assertEqual(len(snapshot.sessions), 4)
        self.assertEqual(len({s.identity for s in snapshot.sessions}), 4)
        self.assertNotIn(
            b"UNSELECTED_PRIVATE", b"".join(f.content for f in plan.writes)
        )

    def test_invalid_or_file_mode_directory_selection_rejected(self):
        for directories in (
            [],
            ["*"],
            ["../x"],
            ["/absolute"],
            ["."],
            ["a//b"],
            ["a", "a"],
            ["a?"],
            ["a\\b"],
        ):
            with self.subTest(directories=directories):
                self.data["sources"][0]["discovery"]["directories"] = directories
                with self.assertRaises(ManifestError):
                    self.load()
        self.data["sources"][0]["discovery"] = {
            "mode": "file",
            "patterns": [],
            "directories": ["selected"],
        }
        with self.assertRaises(ManifestError):
            self.load()


if __name__ == "__main__":
    unittest.main()
