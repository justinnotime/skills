"""Synthetic email rendering, checkpoint safety, and archive compatibility."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from genspark_archive import emails
from genspark_archive.common import ArchiveError, load_config


def record(identity="synthetic-mail-1", **fields):
    return {
        "id": identity,
        "subject": "Example subject",
        "date": "2026-02-03T04:05:06Z",
        "from": "sender@example.invalid",
        "to": "reader@example.invalid",
        "snippet": "Short summary",
        "_full": {"body": "Full message body."},
        **fields,
    }


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    path = tmp_path / "config.json"
    raw = {
        "schema": "genspark-archive/v1",
        "repository_root": str(root),
        "command": [sys.executable],
        "rate_delay": 0,
        "emails": {
            "account": "reader@example.invalid",
            "output_directory": "archive/email",
            "state_file": str(tmp_path / "state.json"),
            "folders": ["inbox"],
            "page_size": 50,
        },
    }
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(emails, "list_emails", lambda *a, **k: [])
    monkeypatch.setattr(emails, "select_folders", lambda client, settings, selected: selected)
    return path, raw, load_config(path, "emails")


def run(configured, *extra):
    return emails.main(
        ["--config", str(configured[0]), "--after", "2026-02-03", "--before", "2026-02-04", *extra]
    )


def test_renderer_preserves_headers_html_links_images_attachments():
    meta = record(cc="copy@example.invalid", hasAttachment=True)
    full = {
        "html_body": '<p>Example <strong>body</strong>.</p><p><a href="https://example.invalid/page">Reference</a></p><img src="https://example.invalid/image.png" alt="Image">',
        "attachments": [
            {
                "name": "file.txt",
                "contentUrl": "https://example.invalid/file",
                "contentType": "text/plain",
                "size": 12,
            }
        ],
        "web_link": "https://outlook.example.invalid/message",
    }
    result = emails.email_to_markdown(meta, full)
    assert result.startswith("# Example subject\n\n- **From:** sender@example.invalid")
    assert "**CC:** copy@example.invalid" in result
    assert "**body**" in result and "[Reference](<https://example.invalid/page>)" in result
    assert "image.png" in result and "## Attachments" in result
    assert "- [file.txt](https://example.invalid/file) (12 bytes) `text/plain`" in result
    assert "Open in Outlook" in result and "`synthetic-mail-1`" in result


def test_explicit_metadata_only_uses_snippet_but_empty_full_body_does_not():
    meta = record(snippet="List summary")
    assert emails.email_to_markdown(meta, None).endswith("List summary")
    assert emails.email_to_markdown(meta, {"body": ""}).endswith("*(no body)*")
    assert emails.email_to_markdown(meta, {"html_body": "Plain response"}).endswith(
        "Plain response"
    )


def test_attachment_alternate_urls_and_missing_download_link():
    text = emails.format_attachment_links(
        [
            {"name": "A", "url": "https://example.invalid/a"},
            {"name": "B", "content_url": "https://example.invalid/b"},
            {"name": "C"},
        ]
    )
    assert "[A](https://example.invalid/a)" in text and "[B](https://example.invalid/b)" in text
    assert "C" in text and "no download link" in text


@pytest.mark.parametrize(
    "when",
    ["2026-02-03T04:05:06Z", "Tue, 03 Feb 2026 04:05:06 +0000", "2026-02-03T04:05:06.987+00:00"],
)
def test_filename_keeps_legacy_datetime_subject_hash(when):
    meta = record(date=when, subject="示例: subject / blocked?")
    suffix = hashlib.md5(meta["id"].encode()).hexdigest()[:8]
    assert emails.filename_for(meta) == f"2026-02-03_0405_示例-subject-blocked_{suffix}.md"
    assert emails.bucket_of(emails.filename_for(meta)) == "2026-02"


def test_unknown_dates_use_undated_bucket_and_safe_filename():
    name = emails.filename_for(record(date="not a timestamp", subject="../escape\\file\x00"))
    assert emails.bucket_of(name) == "undated"
    assert "/" not in name and "\\" not in name and "\0" not in name


def test_state_repairs_only_missing_archives(configured):
    settings = configured[2]
    present, missing = "synthetic-present", "synthetic-missing"
    name = emails.filename_for(record(present))
    output = settings.output_directory / "2026-02" / name
    output.parent.mkdir(parents=True)
    output.write_text("existing message")
    original = {"synced_ids": [present, missing], "last_after": "2026-02-01"}
    repaired = emails.repair_state(settings, original)
    assert repaired["synced_ids"] == [present]
    assert original["synced_ids"] == [present, missing]
    assert repaired["last_after"] == "2026-02-01"


def test_flat_archive_sweep_keeps_provenance_and_months(configured):
    settings = configured[2]
    settings.output_directory.mkdir(parents=True)
    settings.output_directory.joinpath("README.md").write_text("readme")
    settings.output_directory.joinpath("PROVENANCE.md").write_text("provenance")
    name = emails.filename_for(record())
    settings.output_directory.joinpath(name).write_text("email")
    assert emails.sweep_flat_files(settings) == 1
    assert settings.output_directory.joinpath("2026-02", name).read_text() == "email"
    assert settings.output_directory.joinpath("README.md").read_text() == "readme"
    assert settings.output_directory.joinpath("PROVENANCE.md").read_text() == "provenance"


def test_conflicting_flat_copy_does_not_destroy_bucket_content(configured):
    settings = configured[2]
    name = emails.filename_for(record())
    bucket = settings.output_directory / "2026-02"
    bucket.mkdir(parents=True)
    (bucket / name).write_text("existing")
    settings.output_directory.joinpath(name).write_text("different")
    with pytest.raises(ArchiveError):
        emails.sweep_flat_files(settings)
    assert (bucket / name).read_text() == "existing"
    assert settings.output_directory.joinpath(name).read_text() == "different"


def test_success_writes_one_sorted_final_checkpoint(configured, monkeypatch):
    settings = configured[2]
    monkeypatch.setattr(emails, "list_emails", lambda *a, **k: [record("second"), record("first")])
    saves = []
    original = emails.write_state

    def save(path, state):
        saves.append(dict(state))
        original(path, state)

    monkeypatch.setattr(emails, "write_state", save)
    assert run(configured) == 0
    assert len(saves) == 1
    assert json.loads(settings.state_file.read_text())["synced_ids"] == ["first", "second"]
    assert len(list(settings.output_directory.glob("2026-02/*.md"))) == 2


def test_write_failure_after_ten_messages_never_saves_partial_checkpoint(configured, monkeypatch):
    settings = configured[2]
    initial = '{"synced_ids": []}\n'
    settings.state_file.write_text(initial)
    monkeypatch.setattr(
        emails, "list_emails", lambda *a, **k: [record(f"message-{n}") for n in range(12)]
    )
    count = 0
    real_write = emails.write_text

    def write(*args):
        nonlocal count
        count += 1
        if count == 11:
            raise OSError("synthetic write failure")
        return real_write(*args)

    monkeypatch.setattr(emails, "write_text", write)
    assert run(configured) == 1
    assert settings.state_file.read_text() == initial
    assert len(list(settings.output_directory.glob("2026-02/*.md"))) == 10


def test_list_failure_cannot_create_empty_success_checkpoint(configured, monkeypatch):
    monkeypatch.setattr(
        emails,
        "list_emails",
        lambda *a: (_ for _ in ()).throw(ArchiveError("synthetic list failure")),
    )
    assert run(configured) == 1
    assert not configured[2].state_file.exists()
    assert not configured[2].output_directory.exists()


def test_skip_read_does_not_mark_snippet_as_complete(configured, monkeypatch):
    settings = configured[2]
    monkeypatch.setattr(emails, "list_emails", lambda *a, **k: [record()])
    assert run(configured, "--skip-read") == 0
    assert json.loads(settings.state_file.read_text())["synced_ids"] == []
    output = next(settings.output_directory.glob("2026-02/*.md"))
    assert output.read_text().endswith("Short summary")
    monkeypatch.setattr(
        emails, "list_emails", lambda *a, **k: [record(_full={"body": "Complete body"})]
    )
    assert run(configured) == 0
    assert json.loads(settings.state_file.read_text())["synced_ids"] == [record()["id"]]
    assert output.read_text().endswith("Complete body")


def test_existing_archive_is_not_rewritten_but_lost_file_is(configured, monkeypatch):
    settings = configured[2]
    monkeypatch.setattr(emails, "list_emails", lambda *a, **k: [record()])
    assert run(configured) == 0
    real_write = emails.write_text
    monkeypatch.setattr(emails, "write_text", lambda *a: pytest.fail("existing message rewritten"))
    assert run(configured) == 0
    next(settings.output_directory.glob("2026-02/*.md")).unlink()
    writes = []

    def write(*args):
        writes.append(args[0])
        real_write(*args)

    monkeypatch.setattr(emails, "write_text", write)
    assert run(configured) == 0
    assert len(writes) == 1


def test_no_network_doctor_dry_run_and_folder_override(configured, monkeypatch, capsys):
    monkeypatch.setattr(
        emails, "Client", lambda *a: pytest.fail("read-only planning contacted service")
    )
    assert run(configured, "--doctor") == 0
    capsys.readouterr()
    assert run(configured, "--dry-run", "--folders", "sent,drafts") == 0
    assert json.loads(capsys.readouterr().out)["folders"] == ["sent", "drafts"]
    assert not configured[2].state_file.exists()
    assert not configured[2].output_directory.exists()


@pytest.mark.parametrize(
    "extra",
    [
        ("--after", "bad-date"),
        ("--before", "2026-01-01"),
        ("--folders", ""),
        ("--folders=--help",),
    ],
)
def test_invalid_selection_does_not_write(configured, extra):
    assert run(configured, *extra) == 1
    assert not configured[2].state_file.exists()


def test_state_repair_rejects_external_symlink(configured):
    settings = configured[2]
    bucket = settings.output_directory / "2026-02"
    bucket.mkdir(parents=True)
    protected = settings.root.parent / "protected.md"
    protected.write_text("private file")
    bucket.joinpath(emails.filename_for(record())).symlink_to(protected)
    with pytest.raises(ArchiveError):
        emails.repair_state(settings, {"synced_ids": [record()["id"]]})
    assert protected.read_text() == "private file"


def test_entrypoint_accepts_another_home_and_private_config(configured, tmp_path):
    command = Path(__file__).resolve().parents[1] / "scripts/sync-emails"
    alternate_home = tmp_path / "alternate-home"
    alternate_home.mkdir()
    result = subprocess.run(
        [
            str(command),
            "--config",
            str(configured[0]),
            "--dry-run",
            "--after",
            "2026-02-03",
            "--before",
            "2026-02-04",
        ],
        env={
            "HOME": str(alternate_home),
            "PATH": os.environ["PATH"],
            "GENSPARK_ARCHIVE_PYTHON": sys.executable,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["folders"] == ["inbox"]
    assert not configured[2].state_file.exists()


def structured_record(identity="synthetic-mail-1", **fields):
    return {
        "message_id": identity,
        "subject": "Example subject",
        "received_at": "2026-02-03T04:05:06Z",
        "body_coverage": "full",
        "body": {"content_type": "text", "content": "Complete structured message."},
        "body_preview": "Preview",
        "from": {"name": "Sender", "address": "sender@example.invalid"},
        "to": [{"name": "", "address": "reader@example.invalid"}],
        "cc": [],
        "attachments": [],
        "has_attachments": False,
        "web_url": "",
        **fields,
    }


def folder(identity="folder-inbox", name="Inbox", children=0):
    return {
        "folder_id": identity,
        "display_name": name,
        "child_folder_count": children,
        "parent_folder_id": None,
    }


def page(collection, items, cursor=None, **fields):
    data = {
        "success": True,
        "schema_version": 1,
        "source_instance": "reader@example.invalid",
        collection: items,
        "count": len(items),
        "next_cursor": cursor,
        "coverage": {"complete": cursor is None, "dropped_count": 0, "errors": []},
        **fields,
    }
    return {"status": "ok", "data": data}


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def call(self, argv):
        self.calls.append(argv)
        return next(self.responses)

    def pause(self):
        pass


def test_cursor_pages_include_empty_intermediate_page(configured, monkeypatch):
    monkeypatch.undo()
    client = FakeClient(
        [
            page("emails", [structured_record("first")], "one", folder_id="folder-inbox"),
            page("emails", [], "two", folder_id="folder-inbox"),
            page("emails", [structured_record("second")], folder_id="folder-inbox"),
        ]
    )
    result = emails.list_emails(
        client, configured[2], "folder-inbox", date(2026, 2, 3), date(2026, 2, 4)
    )
    assert [item["id"] for item in result] == ["first", "second"]
    assert len(client.calls) == 3
    assert "--cursor" not in client.calls[0]
    assert client.calls[1][-2:] == ["--cursor", "one"]
    assert client.calls[2][-2:] == ["--cursor", "two"]
    assert "--received_after" in client.calls[0] and "--after_date" not in client.calls[0]
    assert all(call[:2] == ["outlook", "list_emails"] for call in client.calls)


def test_repeated_cursor_is_not_silently_complete(configured, monkeypatch):
    monkeypatch.undo()
    client = FakeClient(
        [
            page("emails", [], "same", folder_id="folder-inbox"),
            page("emails", [], "same", folder_id="folder-inbox"),
        ]
    )
    with pytest.raises(ArchiveError, match="repeated a cursor"):
        emails.list_emails(
            client, configured[2], "folder-inbox", date(2026, 2, 3), date(2026, 2, 4)
        )


@pytest.mark.parametrize(
    "patch", [{"dropped_count": 1}, {"errors": ["synthetic-error"]}, {"complete": False}]
)
def test_incomplete_coverage_refuses_success(configured, patch, monkeypatch):
    monkeypatch.undo()
    response = page("emails", [], folder_id="folder-inbox")
    response["data"]["coverage"].update(patch)
    with pytest.raises(ArchiveError, match="incomplete coverage"):
        emails.list_emails(
            FakeClient([response]),
            configured[2],
            "folder-inbox",
            date(2026, 2, 3),
            date(2026, 2, 4),
        )


def test_page_without_explicit_final_cursor_is_invalid(configured, monkeypatch):
    monkeypatch.undo()
    response = page("emails", [], folder_id="folder-inbox")
    del response["data"]["next_cursor"]
    with pytest.raises(ArchiveError, match="completion information"):
        emails.list_emails(
            FakeClient([response]),
            configured[2],
            "folder-inbox",
            date(2026, 2, 3),
            date(2026, 2, 4),
        )


def test_folder_pages_resolve_legacy_names_and_nested_ids(configured, monkeypatch):
    monkeypatch.undo()
    client = FakeClient(
        [
            page("folders", [folder()], "next-folders"),
            page("folders", [folder("sent-id", "Sent Items"), folder("parent-id", "Parent", 1)]),
            page("folders", [folder("nested-id", "Nested")]),
        ]
    )
    assert emails.select_folders(client, configured[2], ["inbox", "sent", "nested-id"]) == [
        "folder-inbox",
        "sent-id",
        "nested-id",
    ]
    assert client.calls[1][-2:] == ["--cursor", "next-folders"]
    assert client.calls[2][-2:] == ["--parent_folder_id", "parent-id"]


def test_ambiguous_folder_name_requires_explicit_id(configured, monkeypatch):
    monkeypatch.undo()
    response = page("folders", [folder("first", "Shared"), folder("second", "Shared")])
    with pytest.raises(ArchiveError, match="missing or ambiguous"):
        emails.select_folders(FakeClient([response]), configured[2], ["Shared"])
    assert emails.select_folders(FakeClient([response]), configured[2], ["second"]) == ["second"]


@pytest.mark.parametrize("coverage", ["preview", "missing"])
def test_partial_body_requires_explicit_skip_read(coverage):
    data = structured_record(body_coverage=coverage, body={"content_type": "text", "content": ""})
    with pytest.raises(ArchiveError, match="body is incomplete"):
        emails.normalize_email(data)
    adapted = emails.normalize_email(data, skip_read=True)
    assert adapted["_full"] is None and adapted["snippet"] == "Preview"


def test_legacy_ids_missing_dates_recipients_and_attachment_metadata():
    raw = structured_record(
        received_at=None,
        from_=None,
        attachments=[{"name": "sample.txt", "content_type": "text/plain", "size_bytes": 13}],
        has_attachments=True,
    )
    raw["from"] = None
    adapted = emails.normalize_email(raw)
    assert adapted["id"] == raw["message_id"]
    assert adapted["date"] == "" and emails.filename_for(adapted).startswith("unknown_")
    assert adapted["from"] == "" and adapted["to"] == "reader@example.invalid"
    rendered = emails.email_to_markdown(adapted, adapted["_full"])
    assert "sample.txt (13 bytes) `text/plain` *(no download link)*" in rendered
    assert "Complete structured message." in rendered


def test_structured_html_and_empty_full_bodies():
    adapted = emails.normalize_email(
        structured_record(body={"content_type": "html", "content": "<p><b>Text</b></p>"})
    )
    assert emails.email_to_markdown(adapted, adapted["_full"]).endswith("**Text**")
    adapted = emails.normalize_email(
        structured_record(body={"content_type": "text", "content": ""})
    )
    assert emails.email_to_markdown(adapted, adapted["_full"]).endswith("*(no body)*")


def test_full_cli_uses_cursor_endpoint_without_read_or_date_pagination(configured, tmp_path):
    path, config_data, settings = configured
    fake = tmp_path / "fake-cli.py"
    calls = tmp_path / "calls.jsonl"
    responses = {
        "list_folders": [page("folders", [folder()])],
        "list_emails": [
            page("emails", [structured_record("first")], "next-emails", folder_id="folder-inbox"),
            page("emails", [structured_record("second")], folder_id="folder-inbox"),
        ],
    }
    response_file = tmp_path / "responses.json"
    response_file.write_text(json.dumps(responses))
    fake.write_text(
        "import json,sys\nfrom pathlib import Path\nresponses=json.loads(Path(sys.argv[1]).read_text())\nargs=sys.argv[3:]\nwith Path(sys.argv[2]).open('a') as output: output.write(json.dumps(args)+'\\n')\nassert args[0]=='outlook'\nassert '--download_attachments' not in args\nindex=1 if '--cursor' in args else 0\nprint(json.dumps(responses[args[1]][index]))\n"
    )
    config_data["command"] = [sys.executable, str(fake), str(response_file), str(calls)]
    path.write_text(json.dumps(config_data))
    script = Path(__file__).resolve().parents[1] / "scripts/sync-emails"
    result = subprocess.run(
        [str(script), "--config", str(path), "--after", "2026-02-03", "--before", "2026-02-04"],
        env=dict(os.environ, GENSPARK_ARCHIVE_PYTHON=sys.executable),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [request[:2] for request in requests] == [
        ["outlook", "list_folders"],
        ["outlook", "list_emails"],
        ["outlook", "list_emails"],
    ]
    assert json.loads(settings.state_file.read_text())["synced_ids"] == ["first", "second"]
    assert len(list(settings.output_directory.glob("2026-02/*.md"))) == 2


def test_different_account_payload_is_rejected(configured, monkeypatch):
    monkeypatch.undo()
    response = page("emails", [], folder_id="folder-inbox", source_instance="other@example.invalid")
    with pytest.raises(ArchiveError, match="source identity"):
        emails.list_emails(
            FakeClient([response]),
            configured[2],
            "folder-inbox",
            date(2026, 2, 3),
            date(2026, 2, 4),
        )


def test_partial_body_does_not_block_complete_messages(configured, monkeypatch, capsys):
    settings = configured[2]
    monkeypatch.undo()
    initial = '{"synced_ids": [], "last_sync": "2026-02-01T00:00:00+00:00"}\n'
    settings.state_file.write_text(initial)
    client = FakeClient(
        [
            page("folders", [folder()]),
            page("emails", [structured_record("first")], "next", folder_id="folder-inbox"),
            page(
                "emails",
                [structured_record("partial", body_coverage="preview")],
                folder_id="folder-inbox",
            ),
        ]
    )
    monkeypatch.setattr(emails, "Client", lambda *a: client)
    assert run(configured) == 0
    state = json.loads(settings.state_file.read_text())
    assert state["synced_ids"] == ["first"]
    assert state["last_sync"] == "2026-02-01T00:00:00+00:00"
    assert state["pending_bodies"] == {
        "partial": {"after": "2026-02-03", "body_coverage": "preview"}
    }
    assert len(list(settings.output_directory.glob("2026-02/*.md"))) == 1
    assert (
        "Complete structured message." in next(settings.output_directory.rglob("*.md")).read_text()
    )
    output = capsys.readouterr()
    assert "WARN email archive" in output.out
    assert 'message_id="partial"' in output.err
    assert "not marked complete" in output.err


def test_pending_body_survives_absence_and_finishes_when_full_body_arrives(configured, monkeypatch):
    settings = configured[2]
    pending = {"partial": {"after": "2000-01-01", "body_coverage": "missing"}}
    settings.state_file.write_text(json.dumps({"synced_ids": [], "pending_bodies": pending}))
    assert run(configured) == 0
    assert json.loads(settings.state_file.read_text())["pending_bodies"] == pending
    assert "last_sync" not in json.loads(settings.state_file.read_text())
    monkeypatch.setattr(
        emails, "list_emails", lambda *a, **k: [record("partial", _full={"text_body": ""})]
    )
    assert run(configured) == 0
    state = json.loads(settings.state_file.read_text())
    assert state["synced_ids"] == ["partial"]
    assert state["pending_bodies"] == {}
    assert state["last_sync"]
    assert next(settings.output_directory.rglob("*.md")).read_text().endswith("*(no body)*")


def test_default_window_retains_pending_bodies_and_outage_history(configured):
    args = SimpleNamespace(after=None, before=None)
    state = {"last_before": "2000-02-02"}
    assert emails.selected_dates(args, configured[2], state)[0] == date(2000, 2, 1)
    state["pending_bodies"] = {"partial": {"after": "2000-01-01", "body_coverage": "missing"}}
    assert emails.selected_dates(args, configured[2], state)[0] == date(2000, 1, 1)
    args.after = "2000-03-01"
    assert emails.selected_dates(args, configured[2], state)[0] == date(2000, 3, 1)


def test_known_full_archive_is_not_downgraded_by_partial_listing(configured, monkeypatch):
    monkeypatch.setattr(emails, "list_emails", lambda *a, **k: [record()])
    assert run(configured) == 0
    monkeypatch.setattr(
        emails, "list_emails", lambda *a, **k: [record(_full=None, _body_coverage="missing")]
    )
    assert run(configured) == 0
    state = json.loads(configured[2].state_file.read_text())
    assert state["synced_ids"] == [record()["id"]]
    assert state["pending_bodies"] == {}


@pytest.mark.parametrize(
    "pending", [[], {"id": {}}, {"id": {"after": "bad", "body_coverage": "missing"}}]
)
def test_invalid_pending_state_is_not_discarded(configured, pending):
    state_file = configured[2].state_file
    initial = json.dumps({"synced_ids": [], "pending_bodies": pending})
    state_file.write_text(initial)
    assert run(configured) == 1
    assert state_file.read_text() == initial


def test_incomplete_pagination_still_prevents_all_checkpoint_changes(configured, monkeypatch):
    monkeypatch.undo()
    settings = configured[2]
    initial = '{"synced_ids": []}\n'
    settings.state_file.write_text(initial)
    bad_page = page("emails", [], folder_id="folder-inbox")
    bad_page["data"]["coverage"]["dropped_count"] = 1
    client = FakeClient(
        [
            page("folders", [folder()]),
            page("emails", [structured_record("first")], "next", folder_id="folder-inbox"),
            bad_page,
        ]
    )
    monkeypatch.setattr(emails, "Client", lambda *a: client)
    assert run(configured) == 1
    assert settings.state_file.read_text() == initial
    assert not settings.output_directory.exists()


def test_malformed_list_cannot_be_treated_as_no_mail(configured, monkeypatch):
    monkeypatch.undo()
    response = page("emails", [], folder_id="folder-inbox")
    response["data"]["count"] = 1
    with pytest.raises(ArchiveError, match="invalid item count"):
        emails.list_emails(
            FakeClient([response]),
            configured[2],
            "folder-inbox",
            date(2026, 2, 3),
            date(2026, 2, 4),
        )


def test_wrong_folder_payload_is_rejected(configured, monkeypatch):
    monkeypatch.undo()
    response = page("emails", [], folder_id="other-folder")
    with pytest.raises(ArchiveError, match="different folder"):
        emails.list_emails(
            FakeClient([response]),
            configured[2],
            "folder-inbox",
            date(2026, 2, 3),
            date(2026, 2, 4),
        )


def test_full_marker_without_body_content_is_not_complete():
    with pytest.raises(ArchiveError, match="full-body record"):
        emails.normalize_email(structured_record(body={"content_type": "text"}))
