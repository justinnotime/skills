import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conftest import synthetic_config

from activity_summary import facts as MODULE
from activity_summary import issue_refs as ISSUE_REFS
from activity_summary.config import activate


def test_document_changes_preserve_history_after_directory_move(tmp_path):
    def git(*args, date=None):
        env = os.environ.copy()
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    old_directory = tmp_path / "sources/old-documents"
    old_document = old_directory / "example/README.md"
    old_document.parent.mkdir(parents=True)
    old_document.write_text("Original document\n")
    project = tmp_path / "knowledge/projects/example/note.md"
    project.parent.mkdir(parents=True)
    project.write_text("Project context\n")
    git("add", ".")
    git("commit", "-qm", "add document", date="2024-01-02T12:00:00Z")
    cfg = synthetic_config(tmp_path)
    cfg["facts"]["document_directory"] = "sources/old-documents"
    activate(cfg)
    before = MODULE.doc_changes(str(tmp_path), "2024-01-02")
    assert before["google_docs"] == [{"doc": "example", "files_changed": 1}]

    git("mv", "sources/old-documents", "sources/documents")
    git("commit", "-qm", "move document directory", date="2024-01-03T12:00:00Z")
    (tmp_path / "sources/documents/example/README.md").write_text("Updated document\n")
    git("add", ".")
    git("commit", "-qm", "update document", date="2024-01-04T12:00:00Z")
    cfg["facts"]["document_directory"] = "sources/documents"
    activate(cfg)
    assert MODULE.doc_changes(str(tmp_path), "2024-01-02")["google_docs"] == []

    cfg["facts"]["document_history_directories"] = ["sources/old-documents"]
    activate(cfg)
    assert not old_directory.exists()
    assert MODULE.doc_changes(str(tmp_path), "2024-01-02") == before
    assert MODULE.doc_changes(str(tmp_path), "2024-01-04")["google_docs"] == [
        {"doc": "example", "files_changed": 1}
    ]


class DailySummaryExtractTest(unittest.TestCase):
    @staticmethod
    def write_mirror(
        root: Path,
        repo: str,
        number: int,
        *,
        created: str = "2026-07-18T00:00:00Z",
        updated: str = "2026-07-20T00:00:00Z",
        closed: str = "",
        merged: str = "",
        extra: str = "",
    ) -> Path:
        directory = root / "sources" / "issues" / repo.replace("/", "_")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{number}.md"
        closed_line = f"closed: '{closed}'\n" if closed else ""
        merged_line = f"merged: '{merged}'\n" if merged else ""
        path.write_text(
            f"---\n{closed_line}created: '{created}'\ncomment_count: 0\n{merged_line}number: {number}\nrepo: {repo}\nstate: open\ntitle: Test {number}\ntype: gh-issue\nupdated: '{updated}'\nurl: https://github.com/{repo}/issues/{number}\n---\n\n# Test {number}\n\n## Body — @author · {created}\n\n{extra}",
            encoding="utf-8",
        )
        return path

    def test_subprocess_failure_is_not_treated_as_empty_facts(self):
        failure = subprocess.CalledProcessError(128, ["git", "log"])
        with mock.patch.object(MODULE.subprocess, "run", side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                MODULE.sh(["git", "log"], "/tmp")

    def test_subprocess_runs_in_checked_mode(self):
        completed = subprocess.CompletedProcess(["git", "log"], 0, "facts\n", "")
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.assertEqual(MODULE.sh(["git", "log"], "/tmp"), "facts\n")
        run.assert_called_once_with(
            ["git", "log"], cwd="/tmp", capture_output=True, text=True, check=True
        )

    def test_canonical_refs_allow_any_positive_number_and_preserve_repo(self):
        self.assertEqual(ISSUE_REFS.canonical("example-org/STORAGE", "01"), "example-org/storage#1")
        self.assertEqual(
            ISSUE_REFS.sort_refs(
                ["example-org/storage#90", "example-org/alpha#440", "example-org/storage#11"]
            ),
            ["example-org/alpha#440", "example-org/storage#11", "example-org/storage#90"],
        )
        with self.assertRaises(ValueError):
            ISSUE_REFS.canonical("example-org/storage", 0)

    def test_gh_touched_requires_source_timestamps_and_ignores_modified_paths(self):
        target = "2026-07-19"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_mirror(root, "example-org/alpha", 92)
            merged = self.write_mirror(
                root, "example-org/storage", 90, merged="2026-07-19T00:40:00Z"
            )
            commented = self.write_mirror(
                root,
                "example-org/storage-bench",
                11,
                extra="### @author · 2026-07-19T00:51:00Z\n\n21x faster\n",
            )
            self.write_mirror(root, "example-org/alpha", 1)
            with mock.patch.object(MODULE, "historical_source_activity", return_value={}):
                facts = MODULE.gh_touched(str(root), target)
            self.assertEqual(
                list(facts), ["example-org/storage#90", "example-org/storage-bench#11"]
            )
            self.assertNotIn("example-org/alpha#92", facts)
            self.assertEqual(facts["example-org/storage#90"]["activity_source"], "current")
            self.assertEqual(
                facts["example-org/storage#90"]["activity_on_target"], ["frontmatter:merged"]
            )
            self.assertEqual(
                facts["example-org/storage-bench#11"]["activity_on_target"], ["comment:created"]
            )
            self.assertEqual(
                facts["example-org/storage#90"]["file"], merged.relative_to(root).as_posix()
            )
            self.assertEqual(
                facts["example-org/storage-bench#11"]["file"],
                commented.relative_to(root).as_posix(),
            )

    def test_gh_touched_recovers_overwritten_source_timestamp_from_later_commit(self):
        target = "2026-07-20"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
            )
            self.write_mirror(
                root,
                "example-org/alpha",
                417,
                created="2026-07-09T15:55:55Z",
                updated="2026-07-20T06:30:00Z",
                extra="### @author · 2026-07-20T06:29:00Z\n\nTarget-day comment\n",
            )
            self.write_mirror(
                root,
                "example-org/alpha",
                386,
                created="2026-06-27T00:00:00Z",
                updated="2026-06-28T00:00:00Z",
                extra="### @author · 2026-06-28T01:00:00Z\n\nOld comment\n",
            )
            env = dict(os.environ)
            env.update(
                {
                    "GIT_AUTHOR_DATE": "2026-07-22T01:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-07-22T01:00:00Z",
                }
            )
            subprocess.run(["git", "add", "sources/issues"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "late initial sync"], cwd=root, env=env, check=True
            )
            self.write_mirror(
                root,
                "example-org/alpha",
                417,
                created="2026-07-09T15:55:55Z",
                updated="2026-07-21T09:39:56Z",
                extra="### @author · 2026-07-21T09:39:56Z\n\nLater comment\n",
            )
            self.write_mirror(
                root,
                "example-org/alpha",
                386,
                created="2026-06-27T00:00:00Z",
                updated="2026-06-28T00:00:00Z",
                extra="### @author · 2026-06-28T01:00:00Z\n\nHydrated formatting only\n",
            )
            env.update(
                {
                    "GIT_AUTHOR_DATE": "2026-07-23T01:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-07-23T01:00:00Z",
                }
            )
            subprocess.run(["git", "add", "sources/issues"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "later mirror sync"], cwd=root, env=env, check=True
            )
            facts = MODULE.gh_touched(str(root), target)
            self.assertEqual(list(facts), ["example-org/alpha#417"])
            recovered = facts["example-org/alpha#417"]
            self.assertEqual(recovered["activity_source"], "git-history")
            self.assertEqual(recovered["activity_on_target"], ["comment:created"])
            self.assertNotIn("example-org/alpha#386", facts)

    def test_commit_refs_use_mirror_candidates_and_project_hint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for number in (70, 74, 80, 90):
                self.write_mirror(root, "example-org/storage", number)
            self.write_mirror(root, "example-org/alpha", 440)
            self.write_mirror(root, "example-org/alpha", 11)
            for number in (11, 28):
                self.write_mirror(root, "example-org/storage-bench", number)
            log = "@@@84df6118|2026-07-19|runbook: queue #70/#74/#80/#440\nknowledge/projects/storage/runbook.md\n@@@abcdef12|2026-07-19|update: record result\nknowledge/projects/storage/result.md\n@@@12345678|2026-07-19|scoreboard: bench#11 and bench#28; storage#70\nknowledge/projects/storage/scoreboard.md\n"
            diff = "diff --git a/knowledge/projects/storage/result.md b/knowledge/projects/storage/result.md\n+++ b/knowledge/projects/storage/result.md\n+PR #90 merged after the gate\n"

            def fake_sh(args, _cwd):
                return log if args[1] == "log" else diff

            with mock.patch.object(MODULE, "sh", side_effect=fake_sh):
                commits = MODULE.commit_entities(str(root), "2026-07-19")
            queue = commits[0]
            self.assertEqual(queue["issues"], ["70", "74", "80", "440"])
            self.assertEqual(
                queue["issue_refs"],
                [
                    "example-org/alpha#440",
                    "example-org/storage#70",
                    "example-org/storage#74",
                    "example-org/storage#80",
                ],
            )
            result = commits[1]
            self.assertEqual(result["issues_in_body"], ["90"])
            self.assertEqual(result["issue_refs_in_body"], ["example-org/storage#90"])
            aliases = commits[2]
            self.assertEqual(
                aliases["issue_refs"],
                [
                    "example-org/storage#70",
                    "example-org/storage-bench#11",
                    "example-org/storage-bench#28",
                ],
            )

    def test_dsh_history_participates_in_session_clusters(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bucket = root / "sources" / "delta-history" / "2026-07"
            bucket.mkdir(parents=True)
            (bucket / "2026-07-19_abcdef123456.md").write_text(
                "# DSH work\n\n- Session ID: `session-dsh`\n- Host: `test-host`\n- Time range: 2026-07-19 10:00:00Z -- 2026-07-19 10:01:00Z\n\n### 2026-07-19 10:00:00Z -- user\n\n> direct work prompt\n\n### 2026-07-19 10:01:00Z -- assistant\n\nreply\n",
                encoding="utf-8",
            )
            previous = os.getcwd()
            try:
                events, metadata = MODULE.session_events(str(root), "2026-07-19")
            finally:
                os.chdir(previous)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][1], "delta-history")
        entry = next(iter(metadata.values()))
        self.assertEqual(entry["prompts"], [(600, "direct work prompt")])

    def test_gh_facts_ignore_later_activity_on_the_same_issue(self):
        target = "2026-07-19"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_mirror(
                root,
                "example-org/storage",
                441,
                updated="2026-07-19T12:00:00Z",
                extra="### @author · 2026-07-19T11:59:00Z\n\nsame-day comment\n",
            )
            with mock.patch.object(MODULE, "historical_source_activity", return_value={}):
                before = MODULE.gh_touched(str(root), target)
            path = self.write_mirror(
                root,
                "example-org/storage",
                441,
                updated="2026-07-23T09:00:00Z",
                closed="2026-07-23T09:00:00Z",
                extra="### @author · 2026-07-19T11:59:00Z\n\nsame-day comment\n### @other · 2026-07-23T08:59:00Z\n\nlater comment\n",
            )
            text = path.read_text(encoding="utf-8")
            text = text.replace("state: open", "state: closed").replace(
                "comment_count: 0", "comment_count: 9"
            )
            path.write_text(text, encoding="utf-8")
            with mock.patch.object(MODULE, "historical_source_activity", return_value={}):
                after = MODULE.gh_touched(str(root), target)
        self.assertEqual(list(before), ["example-org/storage#441"])
        self.assertEqual(before, after)
        for drifting in (
            "state",
            "labels",
            "updated_at",
            "comment_count",
            "closed_at",
            "merged_at",
            "is_epic",
        ):
            self.assertNotIn(drifting, before["example-org/storage#441"])
        self.assertEqual(before["example-org/storage#441"]["title"], "Test 441")

    def test_session_facts_ignore_messages_appended_on_later_days(self):
        target = "2026-07-19"
        head = "# Long session\n\n- Session ID: `session-long`\n- Host: `test-host`\n"
        day_before = "### 2026-07-18 23:50:00Z -- user\n\n> yesterday prompt\n\n### 2026-07-18 23:51:00Z -- assistant\n\nreply\n\n"
        day_t = "### 2026-07-19 10:00:00Z -- user\n\n> direct work prompt\n\n### 2026-07-19 10:01:00Z -- assistant\n\nreply\n\n"
        day_after = "### 2026-07-20 08:00:00Z -- user\n\n> next-day prompt\n\n### 2026-07-20 08:01:00Z -- assistant\n\nreply\n"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bucket = root / "sources" / "assistant-history" / "2026-07"
            bucket.mkdir(parents=True)
            path = bucket / "2026-07-18_session-long.md"
            path.write_text(
                head
                + "- Time range: 2026-07-18 23:50:00Z -- 2026-07-19 10:01:00Z\n\n"
                + day_before
                + day_t,
                encoding="utf-8",
            )
            previous = os.getcwd()
            try:
                before = MODULE.cluster_sessions(str(root), target, 45)
                path.write_text(
                    head
                    + "- Time range: 2026-07-18 23:50:00Z -- 2026-07-20 08:01:00Z\n\n"
                    + day_before
                    + day_t
                    + day_after,
                    encoding="utf-8",
                )
                after = MODULE.cluster_sessions(str(root), target, 45)
            finally:
                os.chdir(previous)
        self.assertEqual(len(before), 1)
        self.assertEqual(before, after)
        cluster = before[0]
        self.assertEqual(cluster["messages"], 2)
        self.assertEqual(cluster["user_prompts"], ["direct work prompt"])
        self.assertEqual(cluster["continued_from"], ["2026-07-18"])
        self.assertNotIn("cross_day_spans", cluster)
        self.assertEqual(cluster["sessions"][0]["started_on"], "2026-07-18")

    def test_session_started_on_prefers_the_writer_started_header(self):
        target = "2026-07-19"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bucket = root / "sources" / "assistant-history" / "2026-07"
            bucket.mkdir(parents=True)
            (bucket / "2026-07-19_dayfile1.md").write_text(
                "# Day two of a long session\n\n- Managed-By: agent-session-extraction/v1\n- Session: session-long\n- Day: 2026-07-19\n- Started: 2026-07-17 22:10:00Z\n- Ended: 2026-07-19 10:01:00Z\n\n---\n\n### 2026-07-19 10:00:00Z — user\n\n> direct work prompt\n\n### 2026-07-19 10:01:00Z — assistant\n\nreply\n",
                encoding="utf-8",
            )
            previous = os.getcwd()
            try:
                clusters = MODULE.cluster_sessions(str(root), target, 45)
            finally:
                os.chdir(previous)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["continued_from"], ["2026-07-17"])
        self.assertEqual(clusters[0]["sessions"][0]["started_on"], "2026-07-17")


if __name__ == "__main__":
    unittest.main()
