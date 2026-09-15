"""Synthetic transport, archive progress, authorization and failure tests."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from genteam import archive, auth, send
from genteam.client import APIError, AuthExpired, Client, channel_label
from genteam.config import ConfigurationError, Settings


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cookie = self.root / "credentials" / "cookie"
        self.cookie.parent.mkdir()
        self.cookie.write_text("synthetic-cookie-value\n")
        self.configuration = self.root / "private config.json"
        self.data = {
            "schema": "genteam/v1",
            "base_url": "https://chat.example.test",
            "cookie_file": str(self.cookie),
            "archive": {
                "output_directory": str(self.root / "archive"),
                "state_file": str(self.root / "state" / "genteam.state.json"),
                "repository_path": "archive/chat",
                "rate_delay": 0,
                "selection": {
                    "enabled": True,
                    "mode": "whitelist",
                    "chats": [{"match": "project"}],
                    "bootstrap_days": 90,
                },
            },
            "send": {"state_directory": str(self.root / "send")},
        }
        self.save_config()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "REPOSITORY_PUBLISH_WORKTREE",
                "REPOSITORY_PUBLISH_STATE",
                "SYNC_STATE_DIR",
                "GENTEAM_CONFIG",
                "GENTEAM_SEND_NO_TTY_OK",
            }
        }
        environment.update(HOME=str(self.root / "home"), XDG_CONFIG_HOME=str(self.root / "config"))
        self.environment = mock.patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.stdout, self.stderr = (io.StringIO(), io.StringIO())
        for stream in (
            contextlib.redirect_stdout(self.stdout),
            contextlib.redirect_stderr(self.stderr),
        ):
            stream.__enter__()
            self.addCleanup(stream.__exit__, None, None, None)
        self.channel = {"id": "ch_project", "name": "project", "channel_type": "group"}

    def save_config(self):
        self.configuration.write_text(json.dumps(self.data))

    def prepare_archive(self):
        archive.configure(
            argparse.Namespace(config=self.configuration, output_dir=None, state_file=None)
        )

    def prepare_send(self):
        send.configure(self.configuration)

    def message(self, mid, text="sample"):
        return {
            "ts": time.strftime("%Y-%m-%dT12:00:00Z", time.gmtime()),
            "kind": "message",
            "data": {
                "comet_message_id": str(mid),
                "sender_display_name": "Example Person",
                "content": text,
            },
        }

    def plan(self, **overrides):
        return {
            "label": "project",
            "server_id": "server-example",
            "auth_channel_id": "ch_project",
            "intercept_channel_id": "ch_project",
            "text": "synthetic message",
            **overrides,
        }


class ConfigurationTests(Fixture):
    def test_missing_endpoint_is_rejected(self):
        self.data.pop("base_url")
        self.save_config()
        with self.assertRaises(ConfigurationError):
            Settings(self.configuration)

    def test_relative_paths_are_relative_to_configuration(self):
        self.data["cookie_file"] = "credentials/cookie"
        self.save_config()
        self.assertEqual(Settings(self.configuration).cookie_file, self.cookie)

    def test_remote_plaintext_transport_is_rejected(self):
        self.data["base_url"] = "http://chat.example.test"
        self.save_config()
        with self.assertRaises(ConfigurationError):
            Settings(self.configuration)

    def test_dm_names_share_one_resolution_rule(self):
        channel = {"channel_type": "dm", "id": "ch_dm", "dm_participants": ["a", {"actor_id": "b"}]}
        self.assertEqual(
            channel_label(channel, {"a": {"display_name": "Zed"}, "b": {"display_name": "Alice"}}),
            "dm-alice-zed",
        )


class TransportTests(Fixture):
    def test_auth_error_does_not_echo_response_credentials(self):
        client = Client(Settings(self.configuration))
        error = urllib.error.HTTPError(
            "https://chat.example.test", 401, "failure", {}, io.BytesIO(b"synthetic-cookie-value")
        )
        with (
            mock.patch("genteam.client.open_request", side_effect=error),
            self.assertRaises(AuthExpired) as caught,
        ):
            client.request("GET", "/servers")
        self.assertNotIn("synthetic-cookie-value", str(caught.exception))

    def test_post_is_not_retried_after_uncertain_transport(self):
        client = Client(Settings(self.configuration))
        with (
            mock.patch(
                "genteam.client.open_request", side_effect=urllib.error.URLError("failed")
            ) as request,
            self.assertRaises(APIError),
        ):
            client.request("POST", "/messages/intercept", body={"sample": True})
        self.assertEqual(request.call_count, 1)

    def test_get_retries_are_bounded(self):
        client = Client(Settings(self.configuration))
        with (
            mock.patch(
                "genteam.client.open_request", side_effect=urllib.error.URLError("failed")
            ) as request,
            mock.patch("time.sleep"),
            self.assertRaises(APIError),
        ):
            client.request("GET", "/servers")
        self.assertEqual(request.call_count, 3)

    def test_cookie_header_control_characters_are_not_echoed(self):
        client = Client(Settings(self.configuration))
        with self.assertRaises(AuthExpired) as caught:
            client.request("GET", "/servers", cookie="secret\nheader")
        self.assertNotIn("secret", str(caught.exception))


class ArchiveTests(Fixture):
    def test_marker_prevents_duplicate_append_after_partial_failure(self):
        self.prepare_archive()
        page = {"items": [self.message(1, "first")], "has_more": False}
        with mock.patch.object(archive, "api_get", return_value=page):
            self.assertEqual(
                archive.fetch_channel(self.channel, "project", "example", {"channels": {}}, 90), 1
            )
            self.assertEqual(
                archive.fetch_channel(self.channel, "project", "example", {"channels": {}}, 90), 0
            )
        content = next((self.root / "archive").rglob("*.md")).read_text()
        self.assertEqual(content.count("<!-- genteam-message: 1 -->"), 1)

    def test_failed_selected_channel_does_not_advance_any_progress(self):
        self.prepare_archive()
        archive.STATE_FILE.parent.mkdir(parents=True)
        original = '{"channels": {"old": {"newest_id": "7"}}}'
        archive.STATE_FILE.write_text(original)
        with (
            mock.patch.object(
                archive, "visible_channels", return_value=[(self.channel, {}, "example")]
            ),
            mock.patch.object(archive, "fetch_channel", side_effect=APIError("synthetic failure")),
        ):
            result = archive.main(["--config", str(self.configuration)])
        self.assertEqual(result, 1)
        self.assertEqual(archive.STATE_FILE.read_text(), original)

    def test_initial_backfill_continues_after_page_limit(self):
        self.data["archive"]["max_pages_per_run"] = 2
        self.save_config()
        self.prepare_archive()
        state = {"channels": {}}
        pages = {
            "": {
                "items": [self.message(5), self.message(6)],
                "has_more": True,
                "oldest_comet_message_id": "5",
            },
            "5": {
                "items": [self.message(3), self.message(4)],
                "has_more": True,
                "oldest_comet_message_id": "3",
            },
            "3": {"items": [self.message(1), self.message(2)], "has_more": False},
        }

        def page(_path, params):
            return pages[params.get("before_message_id", "")]

        with mock.patch.object(archive, "api_get", side_effect=page):
            self.assertEqual(
                archive.fetch_channel(self.channel, "project", "example", state, 90), 4
            )
            self.assertEqual(state["channels"]["ch_project"]["bootstrap_before_id"], "3")
            self.assertEqual(
                archive.fetch_channel(self.channel, "project", "example", state, 90), 2
            )
        self.assertNotIn("bootstrap_before_id", state["channels"]["ch_project"])
        self.assertEqual(state["channels"]["ch_project"]["newest_id"], "6")

    def test_publisher_context_overrides_main_output_and_progress(self):
        with mock.patch.dict(
            os.environ,
            {
                "REPOSITORY_PUBLISH_WORKTREE": str(self.root / "transaction"),
                "REPOSITORY_PUBLISH_STATE": str(self.root / "progress"),
            },
        ):
            self.prepare_archive()
        self.assertEqual(archive.OUTPUT_DIR, self.root / "transaction/archive/chat")
        self.assertEqual(archive.STATE_FILE, self.root / "progress/genteam.state.json")

    def test_publisher_receives_exact_writer_argv(self):
        self.data["publisher"] = {"command": ["/synthetic/publish", "--task", "chat", "--"]}
        self.save_config()
        with mock.patch.object(archive.subprocess, "run") as run:
            run.return_value.returncode = 7
            result = archive.main(["--config", str(self.configuration), "--publish"])
        self.assertEqual(result, 7)
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:4], ["/synthetic/publish", "--task", "chat", "--"])
        self.assertEqual(arguments[-2:], ["--config", str(self.configuration)])
        self.assertNotIn("--publish", arguments)

    def test_thread_failure_is_reported(self):
        self.prepare_archive()
        with (
            mock.patch.object(archive, "list_threads", return_value=[{"id": "thread-example"}]),
            mock.patch.object(archive, "fetch_channel", side_effect=APIError("failed")),
            self.assertRaises(APIError),
        ):
            archive.fetch_channel_threads(self.channel, "project", "example", {"channels": {}}, 90)

    def test_remote_names_cannot_escape_archive_directory(self):
        self.prepare_archive()
        with mock.patch.object(archive, "api_get", return_value={"items": [self.message(1)]}):
            archive.fetch_channel(
                self.channel, "../../project", "../../remote", {"channels": {}}, 90
            )
        for path in self.root.rglob("*.md"):
            self.assertTrue(path.is_relative_to(self.root / "archive"))
        self.assertEqual(archive.month_of("../../x"), "unknown")


class PinnedArchiveTests(Fixture):
    def setUp(self):
        super().setUp()
        self.data["archive"]["selection"] = {"enabled": True, "mode": "pinned"}
        self.save_config()
        self.dm = {"id": "ch_dm", "name": "private-chat", "channel_type": "dm"}
        self.other = {"id": "ch_other", "name": "other", "channel_type": "public"}
        self.resolved = {
            "channels": [self.channel, self.dm, self.other],
            "members": [],
            "viewer": {"pinned_channel_ids": [self.channel["id"], self.dm["id"]]},
        }
        self.message_reads = []

    def request(self, method, path, **kwargs):
        self.assertEqual(method, "GET")
        if path == "/servers":
            return {"servers": [{"id": "server-example", "slug": "example"}]}
        if path == "/servers/resolve":
            return self.resolved
        self.assertRegex(path, r"^/channels/ch_(project|dm|other)/messages$")
        self.message_reads.append(path)
        return {"items": [self.message(len(self.message_reads))], "has_more": False}

    def run_archive(self, *options):
        with mock.patch.object(Client, "request", side_effect=self.request):
            return archive.main(["--config", str(self.configuration), *options])

    def test_pins_follow_ids_and_refresh_each_run_without_removing_history(self):
        self.channel["name"] = "renamed-project"
        self.assertEqual(self.run_archive(), 0)
        self.assertEqual(
            self.message_reads, ["/channels/ch_project/messages", "/channels/ch_dm/messages"]
        )
        old_files = {p: p.read_bytes() for p in (self.root / "archive").rglob("*.md")}
        self.assertEqual(len(old_files), 2)
        self.resolved["viewer"]["pinned_channel_ids"] = [self.other["id"]]
        self.assertEqual(self.run_archive(), 0)
        self.assertEqual(self.message_reads[2:], ["/channels/ch_other/messages"])
        for path, content in old_files.items():
            self.assertEqual(path.read_bytes(), content)
        state = json.loads(archive.STATE_FILE.read_text())["channels"]
        self.assertEqual(state["ch_project"]["newest_id"], "1")
        self.assertEqual(state["ch_dm"]["newest_id"], "2")
        self.assertEqual(state["ch_other"]["newest_id"], "3")

    def test_empty_pins_fetch_no_messages(self):
        self.resolved["viewer"]["pinned_channel_ids"] = []
        self.assertEqual(self.run_archive(), 0)
        self.assertEqual(self.message_reads, [])
        self.assertFalse((self.root / "archive").exists())

    def test_unavailable_or_malformed_pins_fail_without_fetching_or_advancing(self):
        self.prepare_archive()
        archive.STATE_FILE.parent.mkdir(parents=True)
        original = '{"channels": {"ch_project": {"newest_id": "7"}}}'
        archive.STATE_FILE.write_text(original)
        for viewer in [
            None,
            {},
            {"pinned_channel_ids": None},
            {"pinned_channel_ids": "ch_project"},
            {"pinned_channel_ids": [None]},
            {"pinned_channel_ids": [""]},
        ]:
            with self.subTest(viewer=viewer):
                self.resolved["viewer"] = viewer
                self.assertEqual(self.run_archive(), 1)
                self.assertIn("pinned_channel_ids", self.stderr.getvalue())
                self.assertEqual(self.message_reads, [])
                self.assertEqual(archive.STATE_FILE.read_text(), original)
                self.assertFalse((self.root / "archive").exists())

    def test_selected_listing_applies_pins_without_archive_writes(self):
        self.assertEqual(self.run_archive("--list-selected"), 0)
        output = self.stdout.getvalue()
        self.assertIn("ch_project", output)
        self.assertIn("ch_dm", output)
        self.assertNotIn("ch_other", output)
        self.assertEqual(self.message_reads, [])
        self.assertFalse((self.root / "archive").exists())
        self.assertFalse((self.root / "state").exists())

    def test_legacy_selection_does_not_require_pins(self):
        self.resolved.pop("viewer")
        for mode, expected in [("whitelist", ["ch_project"]), ("blacklist", ["ch_dm", "ch_other"])]:
            with self.subTest(mode=mode):
                self.data["archive"]["selection"] = {
                    "enabled": True,
                    "mode": mode,
                    "chats": [{"match": "PrOjEcT"}],
                }
                self.save_config()
                self.stdout.truncate(0)
                self.stdout.seek(0)
                self.assertEqual(self.run_archive("--list-selected"), 0)
                ids = [line.split()[0] for line in self.stdout.getvalue().splitlines()]
                self.assertEqual(ids, expected)
                self.assertEqual(self.message_reads, [])


class SendTests(Fixture):
    def test_preview_reply_to_does_not_create_thread(self):
        with (
            mock.patch.object(
                send, "resolve_target", return_value=(self.channel, "project", "server-example")
            ),
            mock.patch.object(
                send, "backend", side_effect=AssertionError("preview attempted a mutation")
            ),
        ):
            result = send.main(
                [
                    "--config",
                    str(self.configuration),
                    "send",
                    "--to",
                    "project",
                    "--reply-to",
                    "42",
                    "--text",
                    "sample",
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("PREVIEW", self.stdout.getvalue())

    def test_yes_calls_send_once_without_an_implicit_second_confirmation(self):
        with (
            mock.patch.object(
                send, "resolve_target", return_value=(self.channel, "project", "server-example")
            ),
            mock.patch.object(send, "perform_send", return_value="100") as deliver,
        ):
            result = send.main(
                [
                    "--config",
                    str(self.configuration),
                    "send",
                    "--to",
                    "project",
                    "--text",
                    "sample",
                    "--yes",
                ]
            )
        self.assertEqual(result, 0)
        deliver.assert_called_once()

    def test_configured_marker_is_preserved_and_not_duplicated(self):
        self.data["send"]["marker"] = "[assistant]"
        self.save_config()
        self.prepare_send()
        self.assertEqual(send.enforce_marker("sample"), "[assistant] sample")
        self.assertEqual(send.enforce_marker("[assistant] sample"), "[assistant] sample")

    def test_ambiguous_targets_fail(self):
        self.prepare_send()
        with (
            mock.patch.object(
                send,
                "visible_channels",
                return_value=[
                    (self.channel, "project alpha", "server"),
                    (self.channel, "project beta", "server"),
                ],
            ),
            self.assertRaises(SystemExit),
        ):
            send.resolve_target("project")

    def test_transport_success_then_backend_failure_can_recover_without_resend(self):
        self.prepare_send()
        plan = self.plan()
        calls = []

        def backend(method, path, body):
            calls.append(path)
            if path == "/cometchat/auth_info":
                return {"base": {"app_id": "example"}, "comet_group_guid": "group-example"}
            raise APIError("backend unavailable")

        with (
            mock.patch.object(send, "backend", side_effect=backend),
            mock.patch.object(send, "comet_send", return_value="123") as transport,
            self.assertRaisesRegex(APIError, "do not send it again"),
        ):
            send.perform_send(plan)
        transport.assert_called_once()
        pending = send.STATE / "pending-intercepts/123.json"
        self.assertEqual(json.loads(pending.read_text())["comet_message_id"], "123")
        self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 384)
        with (
            mock.patch.object(send, "backend", return_value={"status": "ok"}) as intercept,
            mock.patch.object(send, "comet_send", side_effect=AssertionError("duplicate send")),
        ):
            self.assertEqual(send.perform_send(json.loads(pending.read_text())), "123")
        self.assertEqual(intercept.call_args.args[1], "/messages/intercept")
        self.assertFalse(pending.exists())

    def test_reply_thread_is_created_only_during_send(self):
        self.prepare_send()
        plan = self.plan(pending_parent_channel_id="ch_project", parent_comet_message_id="9")
        replies = [
            {"thread": {"id": "thread-example"}},
            {"base": {"app_id": "example"}, "comet_group_guid": "group-example"},
            {"status": "ok"},
        ]
        with (
            mock.patch.object(send, "backend", side_effect=replies) as backend,
            mock.patch.object(send, "comet_send", return_value="125"),
        ):
            self.assertEqual(send.perform_send(plan), "125")
        self.assertEqual(
            [call.args[1] for call in backend.call_args_list],
            ["/threads", "/cometchat/auth_info", "/messages/intercept"],
        )
        self.assertEqual(backend.call_args.args[2]["channel_id"], "thread-example")

    def test_queue_reject_and_expiry_never_send(self):
        self.prepare_send()
        identifier = "012345abcdef"
        send.write_private_json(
            send.queue_path(identifier), {"created": time.time() - 4000, "plan": self.plan()}
        )
        with (
            mock.patch.object(send, "perform_send", side_effect=AssertionError("unexpected send")),
            self.assertRaises(SystemExit),
        ):
            send.load_proposal(identifier)
        self.assertFalse(send.queue_path(identifier).exists())


class AuthenticationTests(Fixture):
    def test_stdin_stores_private_cookie_without_echo(self):
        with (
            mock.patch.object(sys, "stdin", io.StringIO("fresh-synthetic-cookie\n")),
            mock.patch.object(Client, "request", return_value={"servers": [{"id": "example"}]}),
        ):
            result = auth.main(["--config", str(self.configuration), "--stdin"])
        self.assertEqual(result, 0)
        self.assertEqual(self.cookie.read_text().strip(), "fresh-synthetic-cookie")
        self.assertEqual(stat.S_IMODE(self.cookie.stat().st_mode), 384)
        self.assertNotIn("fresh-synthetic-cookie", self.stdout.getvalue() + self.stderr.getvalue())

    def test_failed_validation_keeps_existing_cookie(self):
        original = self.cookie.read_bytes()
        with (
            mock.patch.object(sys, "stdin", io.StringIO("rejected-synthetic-cookie\n")),
            mock.patch.object(Client, "request", side_effect=AuthExpired("rejected")),
        ):
            self.assertEqual(auth.main(["--config", str(self.configuration), "--stdin"]), 1)
        self.assertEqual(self.cookie.read_bytes(), original)

    def test_non_workspace_json_does_not_count_as_valid_authentication(self):
        with mock.patch.object(Client, "request", return_value={"status": "challenge"}):
            self.assertEqual(auth.main(["--config", str(self.configuration), "--check"]), 1)


if __name__ == "__main__":
    unittest.main()


class IncompleteResponseTests(Fixture):
    def test_missing_workspace_or_channel_list_is_failure(self):
        client = Client(Settings(self.configuration))
        for responses in ([{}], [{"servers": [{"id": "example"}]}, {"members": []}]):
            with (
                self.subTest(responses=responses),
                mock.patch.object(client, "request", side_effect=responses),
                self.assertRaises(APIError),
            ):
                list(client.channels())

    def test_missing_message_list_preserves_durable_progress(self):
        self.prepare_archive()
        archive.STATE_FILE.parent.mkdir(parents=True)
        original = '{"channels": {"ch_project": {"newest_id": "3"}}}'
        archive.STATE_FILE.write_text(original)
        with (
            mock.patch.object(
                archive, "visible_channels", return_value=[(self.channel, {}, "example")]
            ),
            mock.patch.object(archive, "api_get", return_value={}),
        ):
            self.assertEqual(archive.main(["--config", str(self.configuration)]), 1)
        self.assertEqual(archive.STATE_FILE.read_text(), original)

    def test_frontmatter_encodes_untrusted_labels_as_single_scalars(self):
        self.prepare_archive()
        alias = 'name"\nextra: value'
        directory = self.root / "archive/example"
        archive.append_to_month(directory, alias, alias, alias, [self.message(1)])
        header = next(directory.glob("*.md")).read_text().split("---", 2)[1]
        self.assertNotIn("\nextra:", header)
        self.assertIn("chat: " + json.dumps(alias), header)

    def test_incremental_incomplete_cursor_fails(self):
        self.prepare_archive()
        with (
            mock.patch.object(
                archive,
                "api_get",
                return_value={"items": [self.message(2)], "has_more_newer": True},
            ),
            self.assertRaises(APIError),
        ):
            archive.fetch_channel(
                self.channel,
                "project",
                "example",
                {"channels": {"ch_project": {"newest_id": "1"}}},
                90,
            )
