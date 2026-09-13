"""Synthetic end-to-end mirrors, failure checkpoints, and offline rendering."""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
import yaml
from PIL import Image

from google_docs_authority import config, mirror, oauth

DOC_ID = "syntheticDocumentAlpha001"
SECOND_ID = "syntheticDocumentBeta0002"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    sources = root / "sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {"docs": [{"id": DOC_ID, "slug": "sample--syntheti", "title": "Sample"}]}
        )
    )
    path = tmp_path / "config.json"
    raw = {
        "schema": "google-docs-authority/v1",
        "mirror": {
            "repository_root": str(root),
            "source_list": "sources.yaml",
            "discovered_list": "discovered.yaml",
            "output_directory": "archive",
            "cache_directory": str(tmp_path / "cache"),
            "state_file": str(tmp_path / "state.json"),
            "allow_unauthenticated": True,
            "redact_enabled": False,
            "pandoc_command": [sys.executable],
        },
    }
    path.write_text(json.dumps(raw))
    for name in (
        "fetch_html",
        "fetch_markdown",
        "fetch_tab_tree",
        "fetch_inline_object_uris",
        "open_request",
    ):
        monkeypatch.setattr(
            mirror,
            name,
            lambda *a, **k: pytest.fail("unexpected network or export call"),
        )
    monkeypatch.setattr(
        mirror, "drive_meta", lambda *a: {"version": "1", "name": "Sample"}
    )
    monkeypatch.setattr(
        mirror,
        "fetch_tab_tree",
        lambda *a: ("Sample", [{"id": "t.first", "title": "Sample", "children": []}]),
    )
    mirror.configure(config.load(path))
    monkeypatch.setattr(mirror, "OFFLINE", False)
    return path, raw, root


def run(profile, *args):
    return mirror.main(["--config", str(profile[0]), *args])


def configure(profile):
    profile[0].write_text(json.dumps(profile[1]))
    mirror.configure(config.load(profile[0]))


def export(monkeypatch, body):
    monkeypatch.setattr(mirror, "fetch_markdown", lambda *a, **k: body.encode())


def test_native_output_and_version_skip(profile, monkeypatch):
    export(monkeypatch, "# Sample\n\nOriginal words.\n")
    assert run(profile) == 0
    doc = profile[2] / "archive/sample--syntheti"
    body = (doc / "README.md").read_text()
    assert body == mirror.README_HEADER + "# Sample\n\nOriginal words.\n"
    manifest = yaml.safe_load((doc / "manifest.yaml").read_text())
    assert manifest["docId"] == DOC_ID and manifest["layout"] == "single"
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    assert json.loads(checkpoint.read_text())[DOC_ID]["driveVersion"] == "1"
    monkeypatch.setattr(
        mirror,
        "fetch_markdown",
        lambda *a, **k: pytest.fail("unchanged version exported"),
    )
    assert run(profile) == 0
    assert (doc / "README.md").read_text() == body


def test_export_spacers_do_not_rewrite_existing_mirror(profile, monkeypatch):
    export(monkeypatch, "# Sample\n\nOriginal words.\n")
    assert run(profile) == 0
    doc = profile[2] / "archive/sample--syntheti"
    before = {p.name: p.read_bytes() for p in doc.iterdir() if p.is_file()}
    monkeypatch.setattr(
        mirror, "drive_meta", lambda *a: {"version": "2", "name": "Sample"}
    )
    export(monkeypatch, "# Sample\n\n&nbsp;\n\nOriginal words.\n\n&nbsp;\n")
    assert run(profile) == 0
    assert {p.name: p.read_bytes() for p in doc.iterdir() if p.is_file()} == before
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    assert json.loads(checkpoint.read_text())[DOC_ID]["driveVersion"] == "2"


def test_new_export_removes_spacers_without_hiding_word_changes(profile, monkeypatch):
    export(monkeypatch, "# Sample\n\n&nbsp;\n\nOriginal words.\n\n&nbsp;\n")
    assert run(profile) == 0
    readme = profile[2] / "archive/sample--syntheti/README.md"
    assert readme.read_text() == mirror.README_HEADER + "# Sample\n\nOriginal words.\n"
    monkeypatch.setattr(
        mirror, "drive_meta", lambda *a: {"version": "2", "name": "Sample"}
    )
    export(monkeypatch, "# Sample\n\n&nbsp;\n\nChanged words.\n")
    assert run(profile) == 0
    assert "Changed words." in readme.read_text()
    assert "Original words." not in readme.read_text()
    assert "&nbsp;" not in readme.read_text()


@pytest.mark.parametrize("fence", ["```", "````", "~~~"])
def test_export_spacing_preserves_literal_entities(fence):
    literal = (
        "Inline `&nbsp;` and between&nbsp;words.\n\n"
        "    &nbsp;\n\n"
        f"{fence}html\n&nbsp;\n\n\n{fence}\n"
    )
    source = "# Sample\n\n&nbsp;\n\n&nbsp;\n\n" + literal
    expected = "# Sample\n\n" + literal
    assert mirror.normalize_export_spacing(source) == expected
    assert mirror.normalize_export_spacing(expected) == expected
    assert mirror.render_matches_previous(source, expected)
    assert not mirror.render_matches_previous(
        expected, expected.replace("words", "text")
    )


def test_nested_tabs_keep_content_headings_and_links(profile, monkeypatch):
    export(
        monkeypatch,
        "# First\n\n# Internal heading\n\nText.\n\n# Child\n\nMore.\n\n# Second\n\nLast.\n",
    )
    monkeypatch.setattr(
        mirror,
        "fetch_tab_tree",
        lambda *a: (
            "Sample",
            [
                {
                    "id": "t.first",
                    "title": "First",
                    "children": [{"id": "t.child", "title": "Child", "children": []}],
                },
                {"id": "t.second", "title": "Second", "children": []},
            ],
        ),
    )
    assert run(profile) == 0
    doc = profile[2] / "archive/sample--syntheti"
    assert "# Internal heading" in (doc / "first--first/README.md").read_text()
    assert "More." in (doc / "first--first/child--child.md").read_text()
    assert "[First](first--first/README.md)" in (doc / "README.md").read_text()
    assert yaml.safe_load((doc / "manifest.yaml").read_text())["layout"] == "tabs"


def test_title_rename_preserves_identity(profile, monkeypatch):
    export(monkeypatch, "# Sample\n\nOriginal words.\n")
    assert run(profile) == 0
    monkeypatch.setattr(
        mirror, "drive_meta", lambda *a: {"version": "2", "name": "Renamed"}
    )
    monkeypatch.setattr(mirror, "fetch_tab_tree", lambda *a: ("Renamed", []))
    assert run(profile) == 0
    assert not (profile[2] / "archive/sample--syntheti").exists()
    assert (profile[2] / "archive/renamed--syntheti/README.md").is_file()


def test_one_failed_document_keeps_entire_checkpoint(profile, monkeypatch):
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    checkpoint.write_text('{"old": "checkpoint"}\n')
    (profile[2] / "sources.yaml").write_text(
        yaml.safe_dump(
            {
                "docs": [
                    {"id": DOC_ID, "slug": "one", "title": "One"},
                    {"id": SECOND_ID, "slug": "two", "title": "Two"},
                ]
            }
        )
    )

    def sync(doc, state, **kwargs):
        state[doc["id"]] = {"updated": True}
        return "error" if doc["id"] == SECOND_ID else "created"

    monkeypatch.setattr(mirror, "sync_doc", sync)
    assert run(profile) == 1
    assert checkpoint.read_text() == '{"old": "checkpoint"}\n'


def test_unknown_only_does_not_write_state_or_output(profile):
    assert run(profile, "--only", "unknown") == 1
    assert not (profile[2] / "archive").exists()
    assert not Path(profile[1]["mirror"]["state_file"]).exists()


def test_dry_run_does_not_touch_cache_output_or_network(profile, capsys):
    assert run(profile, "--dry-run") == 0
    assert DOC_ID in capsys.readouterr().out
    assert not (profile[2] / "archive").exists()
    assert not Path(profile[1]["mirror"]["cache_directory"]).exists()


@pytest.mark.parametrize(
    ("failure", "diagnostic"),
    [
        (
            HTTPError(
                "https://example.invalid/private", 503, "private detail", {}, None
            ),
            "http-503",
        ),
        (URLError("private detail"), "network"),
        (TimeoutError("private detail"), "timeout"),
    ],
    ids=["http", "network", "timeout"],
)
def test_preflight_transport_failure_exports_and_saves_checkpoint(
    profile, monkeypatch, capsys, failure, diagnostic
):
    export(monkeypatch, "# Sample\n\nOriginal words.\n")
    assert run(profile) == 0
    capsys.readouterr()
    calls = []
    replacement = b"# Sample\n\nUpdated words from the full export.\n"

    def metadata(doc_id, fields):
        assert fields == "version,name,trashed"
        calls.append("preflight")
        raise failure

    def markdown(*args, **kwargs):
        calls.append("export")
        return replacement

    monkeypatch.setattr(mirror, "drive_meta", metadata)
    monkeypatch.setattr(mirror, "fetch_markdown", markdown)
    assert run(profile) == 0
    assert calls == ["preflight", "export"]
    checkpoint = json.loads(Path(profile[1]["mirror"]["state_file"]).read_text())
    assert checkpoint[DOC_ID]["mdSha1"] == hashlib.sha1(replacement).hexdigest()
    assert "driveVersion" not in checkpoint[DOC_ID]
    assert (
        profile[2] / "archive/sample--syntheti/README.md"
    ).read_text() == mirror.README_HEADER + replacement.decode()
    output = capsys.readouterr()
    assert diagnostic in output.err
    assert "private detail" not in output.out + output.err
    assert "example.invalid" not in output.out + output.err


def test_preflight_and_body_failure_preserve_checkpoint(profile, monkeypatch):
    export(monkeypatch, "# Sample\n\nOriginal words.\n")
    assert run(profile) == 0
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    before = checkpoint.read_bytes()
    readme = profile[2] / "archive/sample--syntheti/README.md"
    old_body = readme.read_bytes()
    calls = []

    def metadata(*args):
        calls.append("preflight")
        raise URLError("synthetic outage")

    def markdown(*args, **kwargs):
        calls.append("export")
        raise HTTPError("https://example.invalid", 404, "unavailable", {}, None)

    monkeypatch.setattr(mirror, "drive_meta", metadata)
    monkeypatch.setattr(mirror, "fetch_markdown", markdown)
    assert run(profile) == 1
    assert calls == ["preflight", "export"]
    assert checkpoint.read_bytes() == before
    assert readme.read_bytes() == old_body


@pytest.mark.parametrize(
    "failure",
    [
        oauth.OAuthError("token-file-unreadable-or-invalid"),
        ValueError("mirror-drive-metadata-invalid"),
    ],
    ids=["oauth", "metadata-schema"],
)
def test_preflight_configuration_failure_does_not_export(profile, monkeypatch, failure):
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    checkpoint.write_text('{"previous": "checkpoint"}\n')
    before = checkpoint.read_bytes()
    exports = []

    def metadata(*args):
        raise failure

    def markdown(*args, **kwargs):
        exports.append(args)
        return b"# Sample\n\nUnexpected full export.\n"

    monkeypatch.setattr(mirror, "drive_meta", metadata)
    monkeypatch.setattr(mirror, "fetch_markdown", markdown)
    assert run(profile) == 1
    assert exports == []
    assert checkpoint.read_bytes() == before


def test_export_links_metadata_failure_keeps_checkpoint(profile, monkeypatch):
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    checkpoint.write_text('{"previous": "checkpoint"}\n')
    before = checkpoint.read_bytes()
    calls = []

    def metadata(doc_id, fields):
        calls.append(fields)
        if fields == "version,name,trashed":
            return {"version": "1", "name": "Sample"}
        assert fields == "exportLinks"
        raise URLError("synthetic outage")

    def request(*args, **kwargs):
        raise HTTPError(
            "https://example.invalid",
            403,
            "export limit",
            {},
            io.BytesIO(b'{"error":{"reason":"exportSizeLimitExceeded"}}'),
        )

    monkeypatch.setattr(mirror, "drive_meta", metadata)
    monkeypatch.setattr(mirror, "get_access_token", lambda: "synthetic-access")
    monkeypatch.setattr(mirror, "open_request", request)
    monkeypatch.setattr(
        mirror,
        "fetch_markdown",
        lambda doc_id, *a, **k: mirror.fetch_export_authenticated(
            doc_id, "text/markdown"
        ),
    )
    assert run(profile) == 1
    assert calls == ["version,name,trashed", "exportLinks"]
    assert checkpoint.read_bytes() == before


def test_markdown_server_failure_is_not_hidden_by_html_fallback(profile, monkeypatch):
    monkeypatch.setattr(
        mirror,
        "fetch_markdown",
        lambda *a, **k: (_ for _ in ()).throw(
            HTTPError("https://example.invalid", 500, "failure", {}, None)
        ),
    )
    assert run(profile) == 1
    assert not Path(profile[1]["mirror"]["state_file"]).exists()


def test_html_fallback_preserves_legacy_header(profile, monkeypatch):
    profile[1]["mirror"]["readme_header"] = (
        "<!-- Caller-selected mirror header. -->\n\n"
    )
    configure(profile)
    monkeypatch.setattr(mirror, "fetch_markdown", lambda *a, **k: None)
    monkeypatch.setattr(
        mirror,
        "fetch_html",
        lambda *a, **k: b'<html><p class="title" id="h.synthetic">Sample</p></html>',
    )
    monkeypatch.setattr(mirror, "run_pandoc", lambda *a: "# Sample\n\nHTML content.\n")
    assert run(profile) == 0
    assert (
        (profile[2] / "archive/sample--syntheti/README.md")
        .read_text()
        .startswith(profile[1]["mirror"]["readme_header"])
    )


def test_html_image_shrink_refuses_and_removes_new_images(profile, monkeypatch):
    doc = profile[2] / "archive/sample--syntheti"
    attachments = doc / "attachments"
    attachments.mkdir(parents=True)
    old = [{"sha1": f"{i:040x}", "ext": "png"} for i in range(3)]
    for image in old:
        (attachments / f"{image['sha1']}.png").write_bytes(b"x" * 300_000)
    (doc / "manifest.yaml").write_text(yaml.safe_dump({"docId": DOC_ID, "images": old}))
    (doc / "README.md").write_text("Old content.\n")
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    checkpoint.write_text("{}\n")
    monkeypatch.setattr(
        mirror,
        "fetch_html",
        lambda *a, **k: b'<p class="title" id="h.synthetic">Sample</p>',
    )

    def pandoc(*args):
        for i in range(3):
            (attachments / f"{i + 100:040x}.png").write_bytes(b"x" * 10_000)
        return "Different content.\n" + "\n".join(
            f"![](attachments/{i + 100:040x}.png)" for i in range(3)
        )

    monkeypatch.setattr(mirror, "run_pandoc", pandoc)
    assert run(profile, "--engine", "html", "--force") == 1
    assert checkpoint.read_text() == "{}\n"
    assert (doc / "README.md").read_text() == "Old content.\n"
    assert len(list(attachments.iterdir())) == 3


def test_crawl_adds_and_syncs_discovered_documents(profile, monkeypatch):
    def sync(doc, state, **kwargs):
        state[doc["id"]] = {"created": True}
        directory = mirror.OUTPUT_DIR / doc["slug"]
        directory.mkdir(parents=True, exist_ok=True)
        directory.joinpath("README.md").write_text(
            f"https://docs.google.com/document/d/{SECOND_ID}/edit\n"
        )
        return "created"

    monkeypatch.setattr(mirror, "sync_doc", sync)
    monkeypatch.setattr(
        mirror,
        "fetch_html",
        lambda *a: (
            b'<title>Discovered</title><p class="title" id="h.synthetic">Discovered</p>'
        ),
    )
    assert run(profile, "--crawl") == 0
    discovered = yaml.safe_load((profile[2] / "discovered.yaml").read_text())
    assert discovered["docs"][0]["id"] == SECOND_ID
    assert set(json.loads(Path(profile[1]["mirror"]["state_file"]).read_text())) == {
        DOC_ID,
        SECOND_ID,
        "_inaccessible",
    }


def test_crawl_transport_failure_does_not_checkpoint(profile, monkeypatch):
    export(
        monkeypatch,
        f"# Sample\n\nhttps://docs.google.com/document/d/{SECOND_ID}/edit\n",
    )
    monkeypatch.setattr(
        mirror,
        "fetch_html",
        lambda *a: (_ for _ in ()).throw(
            HTTPError("https://example.invalid", 500, "failure", {}, None)
        ),
    )
    assert run(profile, "--crawl") == 1
    assert not Path(profile[1]["mirror"]["state_file"]).exists()
    assert not (profile[2] / "discovered.yaml").exists()


def test_failures_name_the_document_and_status_survives_failed_transaction(
    profile, monkeypatch, capsys
):
    status_file = profile[0].parent / "status.json"
    profile[1]["mirror"]["status_file"] = str(status_file)
    configure(profile)
    (profile[2] / "sources.yaml").write_text(
        yaml.safe_dump(
            {"docs": [{"id": DOC_ID, "slug": "one"}, {"id": SECOND_ID, "slug": "two"}]}
        )
    )
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    checkpoint.write_text('{"previous": "published checkpoint"}\n')
    before = checkpoint.read_bytes()

    def sync(doc, state, **kwargs):
        state[doc["id"]] = {"updated": True}
        if doc["id"] == SECOND_ID:
            raise HTTPError(
                "https://example.invalid?token=private-token",
                404,
                "private-message",
                {},
                io.BytesIO(b"private-body"),
            )
        return "updated"

    monkeypatch.setattr(mirror, "sync_doc", sync)
    assert run(profile) == 1
    assert checkpoint.read_bytes() == before
    status = json.loads(status_file.read_text())["documents"]
    assert status[DOC_ID]["last_success_at"]
    assert status[SECOND_ID]["consecutive_failures"] == 1
    assert status[SECOND_ID]["http_status"] == 404
    output = capsys.readouterr()
    assert (
        f"FAIL document={SECOND_ID} category=inaccessible http_status=404" in output.err
    )
    assert f"FAIL document={DOC_ID}" not in output.err
    assert all(
        secret not in output.out + output.err + status_file.read_text()
        for secret in (
            "example.invalid",
            "private-token",
            "private-message",
            "private-body",
        )
    )
    assert run(profile) == 1
    assert (
        json.loads(status_file.read_text())["documents"][SECOND_ID][
            "consecutive_failures"
        ]
        == 2
    )
    monkeypatch.setattr(mirror, "sync_doc", lambda *a, **k: "unchanged")
    assert run(profile) == 0
    assert (
        f"document={SECOND_ID} recovered after 2 failed inspection(s)"
        in capsys.readouterr().out
    )


def test_status_is_read_only_and_independent_of_bad_checkpoint(
    profile, monkeypatch, capsys
):
    status_file = profile[0].parent / "status.json"
    profile[1]["mirror"]["status_file"] = str(status_file)
    configure(profile)
    Path(profile[1]["mirror"]["state_file"]).write_text("malformed checkpoint")
    monkeypatch.setattr(
        mirror, "sync_doc", lambda *a, **k: pytest.fail("status invoked writer")
    )
    assert run(profile, "--status", "--only", DOC_ID) == 0
    assert json.loads(capsys.readouterr().out)["documents"] == {DOC_ID: None}
    assert not status_file.exists()
    assert not (profile[2] / "archive").exists()


def test_offline_render_does_not_change_live_access_status(profile, monkeypatch):
    status_file = profile[0].parent / "status.json"
    profile[1]["mirror"]["status_file"] = str(status_file)
    configure(profile)
    monkeypatch.setattr(mirror, "sync_doc", lambda *a, **k: "unchanged")
    assert run(profile, "--from-cache") == 0
    assert not status_file.exists()


def test_status_cannot_overwrite_transaction_checkpoint(profile):
    status_file = profile[0].parent / "status.json"
    profile[1]["mirror"]["status_file"] = str(status_file)
    configure(profile)
    assert run(profile, "--state-file", str(status_file)) == 1
    assert not status_file.exists()


@pytest.mark.parametrize(
    "other_mode",
    ["--setup-cache", "--doctor", "--dry-run", "--crawl", "--force", "--from-cache"],
)
def test_status_rejects_conflicting_modes_without_side_effects(profile, other_mode):
    assert run(profile, "--status", other_mode) == 1
    assert not Path(profile[1]["mirror"]["cache_directory"]).exists()
    assert not (profile[2] / "archive").exists()


@pytest.mark.parametrize("http_status", [401, 403, 404])
def test_preflight_access_failure_does_not_try_export(
    profile, monkeypatch, http_status, capsys
):
    def metadata(*args):
        raise HTTPError(
            "https://example.invalid", http_status, "private-detail", {}, None
        )

    monkeypatch.setattr(mirror, "drive_meta", metadata)
    monkeypatch.setattr(
        mirror,
        "fetch_markdown",
        lambda *a, **k: pytest.fail("export after rejected access"),
    )
    assert run(profile) == 1
    assert f"document={DOC_ID}" in capsys.readouterr().err
    assert not Path(profile[1]["mirror"]["state_file"]).exists()


def test_explicit_trash_retains_archive_selection_and_checkpoint(
    profile, monkeypatch, capsys
):
    export(monkeypatch, "# Sample\n\nOriginal words.\n")
    assert run(profile) == 0
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    before = checkpoint.read_bytes()
    document = profile[2] / "archive/sample--syntheti/README.md"
    original = document.read_bytes()
    sources = (profile[2] / "sources.yaml").read_bytes()
    monkeypatch.setattr(
        mirror,
        "drive_meta",
        lambda *a: {"version": "2", "name": "Sample", "trashed": True},
    )
    assert run(profile) == 1
    assert f"document={DOC_ID} category=trashed" in capsys.readouterr().err
    assert document.read_bytes() == original
    assert checkpoint.read_bytes() == before
    assert (profile[2] / "sources.yaml").read_bytes() == sources


@pytest.mark.parametrize("age_hours, expected_probe", [(1, False), (25, True)])
def test_crawl_rechecks_inaccessible_after_cooldown(
    profile, monkeypatch, age_hours, expected_probe
):
    output = profile[2] / "archive/one"
    output.mkdir(parents=True)
    (output / "README.md").write_text(
        f"https://docs.google.com/document/d/{SECOND_ID}/edit\n"
    )
    checked_at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    state = {
        "_inaccessible": {
            SECOND_ID: {"checked_at": checked_at, "reason": "fetch failed"}
        }
    }
    probes = []

    def fetch(doc_id):
        probes.append(doc_id)
        return (
            b'<title>Recovered</title><p class="title" id="h.synthetic">Recovered</p>'
        )

    monkeypatch.setattr(mirror, "fetch_html", fetch)
    result = mirror.crawl_for_new_docs(state)
    assert bool(result) == expected_probe
    assert probes == ([SECOND_ID] if expected_probe else [])
    assert (SECOND_ID in state["_inaccessible"]) != expected_probe


def test_rendering_helpers_preserve_code_and_unicode():
    assert mirror.title_slug("示例 Document") == "示例-document"
    assert (
        mirror.fuse_code_line_runs("`if x:`  \n`    y()`  \n")
        == "```\nif x:\n    y()\n```\n"
    )
    assert (
        mirror.postprocess_native_md(
            "[link](https://www.google.com/url?q=https%3A%2F%2Fexample.invalid&sa=D)\n"
        )
        == "[link](https://example.invalid)\n"
    )
    assert (
        mirror.inline_image_refs("![][asset]\n[asset]: attachments/abc.png\n")
        == "![](attachments/abc.png)\n"
    )


@pytest.mark.parametrize(
    "value", ["../escape", "/absolute", "nested/file", "bad\\file", ".."]
)
def test_tab_ids_cannot_escape_output(value):
    with pytest.raises(ValueError):
        mirror.tab_basename("Tab", value)


def test_stale_manifest_cannot_delete_outside_document(tmp_path):
    doc = tmp_path / "doc"
    doc.mkdir()
    outside = tmp_path / "protected.md"
    outside.write_text("keep")
    manifest = doc / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({"tabs": [{"path": "../protected.md"}]}))
    with pytest.raises(ValueError):
        mirror.cleanup_stale_tabs(doc, manifest, set())
    assert outside.read_text() == "keep"


def test_redaction_command_failure_never_returns_unredacted(
    profile, monkeypatch, capsys
):
    monkeypatch.setattr(mirror, "MASK_ENABLED", True)
    monkeypatch.setattr(
        mirror,
        "REDACT_COMMAND",
        [
            sys.executable,
            "-c",
            "import sys;print('syntheticPrivateText',file=sys.stderr);sys.exit(3)",
        ],
    )
    with pytest.raises(ValueError, match="mirror-redaction-command-failed"):
        mirror.redact_secrets("private document")
    assert "syntheticPrivateText" not in capsys.readouterr().err


def test_redaction_protocol_and_tier_override(profile, monkeypatch):
    script = profile[2] / "redactor.py"
    script.write_text(
        "import json,sys\ntext=sys.stdin.read()\nassert sys.argv[1]=='ctx,hard,heur'\nprint(json.dumps({'text':text.replace('synthetic-secret','[MASKED]'),'replacements':1}))\n"
    )
    monkeypatch.setattr(mirror, "MASK_ENABLED", True)
    monkeypatch.setattr(
        mirror, "REDACT_COMMAND", [sys.executable, str(script), "@tiers@"]
    )
    assert (
        mirror.redact_secrets("synthetic-secret", {"hard", "ctx", "heur"}) == "[MASKED]"
    )


def test_cli_cannot_disable_required_redaction(profile):
    profile[1]["mirror"].update(
        redact_enabled=True,
        redact_command=[sys.executable, "-c", "raise SystemExit(3)"],
    )
    configure(profile)
    assert run(profile, "--no-mask") == 1


def test_cache_setup_rejects_other_target(profile):
    profile[1]["mirror"]["cache_link"] = ".export-cache"
    configure(profile)
    wrong = profile[2].parent / "wrong-cache"
    wrong.mkdir()
    (profile[2] / ".export-cache").symlink_to(wrong)
    assert run(profile, "--setup-cache") == 1
    assert (profile[2] / ".export-cache").resolve() == wrong


def test_cache_setup_and_doctor_are_network_free(profile):
    profile[1]["mirror"]["cache_link"] = ".export-cache"
    configure(profile)
    assert run(profile, "--setup-cache") == 0
    assert (profile[2] / ".export-cache").resolve() == Path(
        profile[1]["mirror"]["cache_directory"]
    )
    assert run(profile, "--doctor") == 0


def test_cache_files_are_private(tmp_path):
    path = tmp_path / "cache" / "export.md"
    mirror.write_private_cache(path, b"synthetic document")
    assert path.stat().st_mode & 0o777 == 0o600


def test_offline_cli_does_not_fetch_and_missing_cache_fails(profile):
    cache = Path(profile[1]["mirror"]["cache_directory"]) / "sample--syntheti"
    cache.mkdir(parents=True)
    cache.joinpath("last-export.md").write_text("# Sample\n\nOffline text.\n")
    script = Path(__file__).resolve().parents[1] / "scripts/sync"
    environment = dict(os.environ, GOOGLE_DOCS_AUTHORITY_PYTHON=sys.executable)
    result = subprocess.run(
        [str(script), "--config", str(profile[0]), "--from-cache"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cache.joinpath("last-export.md").unlink()
    before = Path(profile[1]["mirror"]["state_file"]).read_bytes()
    result = subprocess.run(
        [str(script), "--config", str(profile[0]), "--from-cache"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 1
    assert Path(profile[1]["mirror"]["state_file"]).read_bytes() == before


def png(size):
    data = io.BytesIO()
    Image.new("RGB", size, "red").save(data, format="PNG")
    return data.getvalue()


def test_original_image_upgrade_matches_pixels(profile, monkeypatch):
    directory = mirror.cache_dir_for("sample--syntheti")
    directory.mkdir(parents=True)
    small, original = png((2, 2)), png((8, 8))
    md, images = mirror.extract_md_images(
        "![](data:image/png;base64," + base64.b64encode(small).decode() + ")",
        "sample--syntheti",
    )
    monkeypatch.setattr(
        mirror,
        "fetch_inline_object_uris",
        lambda *a: ["https://asset.googleusercontent.com/original"],
    )
    monkeypatch.setattr(mirror, "get_access_token", lambda: "syntheticToken")
    monkeypatch.setattr(mirror, "open_request", lambda *a, **k: io.BytesIO(original))
    converted, final, stats = mirror.upgrade_images_to_originals(
        DOC_ID, md, images, "sample--syntheti"
    )
    original_hash = hashlib.sha1(original).hexdigest()
    assert original_hash in converted and final[0]["sha1"] == original_hash
    assert "1 upgraded" in stats


@pytest.mark.parametrize(
    "url",
    [
        "http://docs.google.com/export",
        "https://example.invalid/steal",
        "https://docs.google.com@evil.invalid/export",
        "https://docs.google.com:444/export",
        "https://localhost/export",
    ],
)
def test_google_asset_url_refuses_non_google_or_unsafe_endpoints(url):
    with pytest.raises(ValueError):
        mirror.google_asset_url(url)


def test_configured_token_failure_never_falls_back_to_anonymous(profile, monkeypatch):
    from google_docs_authority import oauth

    profile[1]["read_token_file"] = str(profile[2].parent / "missing-token.json")
    configure(profile)
    monkeypatch.setattr(
        oauth,
        "refresh_access_token",
        lambda *a: (_ for _ in ()).throw(oauth.OAuthError("token-unreadable")),
    )
    with pytest.raises(oauth.OAuthError):
        mirror.get_access_token()


def test_original_image_external_host_never_receives_bearer(profile, monkeypatch):
    directory = mirror.cache_dir_for("sample--syntheti")
    directory.mkdir(parents=True)
    md, images = mirror.extract_md_images(
        "![](data:image/png;base64," + base64.b64encode(png((2, 2))).decode() + ")",
        "sample--syntheti",
    )
    monkeypatch.setattr(
        mirror,
        "fetch_inline_object_uris",
        lambda *a: ["https://example.invalid/private-token-target"],
    )
    monkeypatch.setattr(mirror, "get_access_token", lambda: "syntheticToken")
    with pytest.raises(ValueError, match="mirror-google-asset-url-invalid"):
        mirror.upgrade_images_to_originals(DOC_ID, md, images, "sample--syntheti")


def test_original_download_failure_does_not_accept_downsampled_bytes(
    profile, monkeypatch
):
    directory = mirror.cache_dir_for("sample--syntheti")
    directory.mkdir(parents=True)
    md, images = mirror.extract_md_images(
        "![](data:image/png;base64," + base64.b64encode(png((2, 2))).decode() + ")",
        "sample--syntheti",
    )
    monkeypatch.setattr(
        mirror,
        "fetch_inline_object_uris",
        lambda *a: ["https://asset.googleusercontent.com/original"],
    )
    monkeypatch.setattr(mirror, "get_access_token", lambda: "syntheticToken")
    monkeypatch.setattr(
        mirror,
        "open_request",
        lambda *a, **k: (_ for _ in ()).throw(URLError("unavailable")),
    )
    with pytest.raises(ValueError, match="mirror-original-image-fetch-failed"):
        mirror.upgrade_images_to_originals(DOC_ID, md, images, "sample--syntheti")


def test_malformed_source_diagnostics_do_not_echo_private_text(profile, capsys):
    (profile[2] / "sources.yaml").write_text("docs: [synthetic-private-text:\n")
    assert run(profile) == 1
    assert "synthetic-private-text" not in capsys.readouterr().err


def test_document_symlink_cannot_write_outside_archive(profile, monkeypatch):
    archive = profile[2] / "archive"
    archive.mkdir()
    outside = profile[2].parent / "protected"
    outside.mkdir()
    archive.joinpath("sample--syntheti").symlink_to(outside)
    export(monkeypatch, "# Sample\n\nContent.\n")
    assert run(profile) == 1
    assert list(outside.iterdir()) == []


def test_invalid_redaction_response_never_persists_raw_body(profile, monkeypatch):
    profile[1]["mirror"].update(
        redact_enabled=True, redact_command=[sys.executable, "-c", "print('not-json')"]
    )
    configure(profile)
    export(monkeypatch, "# Sample\n\nsynthetic-private-text\n")
    assert run(profile) == 1
    assert not (profile[2] / "archive/sample--syntheti/README.md").exists()
    assert not Path(profile[1]["mirror"]["state_file"]).exists()


@pytest.mark.parametrize("target", ["config", "repository"])
def test_state_override_cannot_overwrite_config_or_repository(
    profile, monkeypatch, target
):
    export(monkeypatch, "# Sample\n\nContent.\n")
    destination = profile[0] if target == "config" else profile[2] / "output-state.json"
    before = profile[0].read_text()
    assert run(profile, "--state-file", str(destination)) == 1
    assert profile[0].read_text() == before
    assert not (profile[2] / "output-state.json").exists()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tabs": []},
        {"title": "Sample", "tabs": [{}]},
        {
            "title": "Sample",
            "tabs": [
                {"tabProperties": {"tabId": "t.id", "title": "Tab"}, "childTabs": {}}
            ],
        },
    ],
)
def test_partial_api_tab_metadata_is_rejected(profile, monkeypatch, body):
    monkeypatch.undo()
    mirror.configure(config.load(profile[0]))
    monkeypatch.setattr(mirror, "get_access_token", lambda: "syntheticToken")
    monkeypatch.setattr(
        mirror, "open_request", lambda *a, **k: io.BytesIO(json.dumps(body).encode())
    )
    with pytest.raises(ValueError):
        mirror.fetch_tab_tree(DOC_ID)
    with pytest.raises(ValueError):
        mirror.fetch_inline_object_uris(DOC_ID)


def test_bad_tab_response_preserves_existing_files_and_state(profile, monkeypatch):
    export(monkeypatch, "# Sample\n\nExisting content.\n")
    assert run(profile) == 0
    directory = profile[2] / "archive/sample--syntheti"
    before = {
        str(p.relative_to(directory)): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    }
    checkpoint = Path(profile[1]["mirror"]["state_file"])
    state_before = checkpoint.read_bytes()
    monkeypatch.setattr(mirror, "fetch_tab_tree", lambda *a: mirror.validated_tabs({}))
    export(monkeypatch, "# Sample\n\nChanged content.\n")
    assert run(profile, "--force") == 1
    assert checkpoint.read_bytes() == state_before
    assert {
        str(p.relative_to(directory)): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    } == before


def test_cache_slug_symlink_cannot_escape_configured_cache(profile):
    cache = Path(profile[1]["mirror"]["cache_directory"])
    cache.mkdir()
    protected = cache.parent / "protected-cache"
    protected.mkdir()
    (cache / "sample--syntheti").symlink_to(protected)
    with pytest.raises(ValueError):
        mirror.md_cache_path("sample--syntheti")
    with pytest.raises(ValueError):
        mirror.html_cache_path("sample--syntheti")
    assert list(protected.iterdir()) == []


def test_real_pandoc_extracts_synthetic_html_image(tmp_path, monkeypatch):
    import shutil

    pandoc = shutil.which("pandoc")
    if not pandoc:
        pytest.skip("pandoc is a declared system dependency")
    monkeypatch.setattr(mirror, "PANDOC_COMMAND", [pandoc])
    monkeypatch.setattr(mirror, "PANDOC_MEM_MAX", None)
    data = png((3, 3))
    html = (
        '<html><body><h1>Sample</h1><p>Example text.</p><img src="data:image/png;base64,'
        + base64.b64encode(data).decode()
        + '"></body></html>'
    ).encode()
    rendered = mirror.run_pandoc(html, tmp_path)
    assert "Sample" in rendered and "Example text." in rendered
    images = mirror.parse_images_from_md(rendered)
    assert len(images) == 1
    attachment = tmp_path / "attachments" / (images[0]["sha1"] + "." + images[0]["ext"])
    assert attachment.read_bytes() == data
