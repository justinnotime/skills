"""Archive complete Outlook messages through the configured Genspark CLI.

Structured folder and email endpoints provide explicit continuation cursors
and body coverage. Complete messages can advance independently; incomplete
bodies stay pending until the source supplies their full text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import html2text

from .common import (
    ArchiveError,
    Client,
    add_common_arguments,
    load_config,
    output_path,
    read_state,
    write_state,
    write_text,
)


def _pages(client, settings, operation, collection, extra):
    """Read documented cursor pages; never infer completion from page length."""
    cursor = None
    seen = set()
    source = None
    while True:
        args = [
            "outlook",
            operation,
            "--from_account",
            settings.account,
            "--page_size",
            str(settings.options.get("page_size", 50)),
            *extra,
        ]
        if cursor is not None:
            args.extend(["--cursor", cursor])
        response = client.call(args)
        payload = response.get("data")
        if (
            not isinstance(payload, dict)
            or payload.get("success") is not True
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
        ):
            raise ArchiveError("structured Outlook response is invalid")
        batch = payload.get(collection)
        if (
            not isinstance(batch, list)
            or type(payload.get("count")) is not int
            or payload["count"] != len(batch)
        ):
            raise ArchiveError("structured Outlook response has an invalid item count")
        if "next_cursor" not in payload:
            raise ArchiveError("structured Outlook response omitted completion information")
        following = payload["next_cursor"]
        if following is not None and (not isinstance(following, str) or not following):
            raise ArchiveError("structured Outlook response has an invalid cursor")
        coverage = payload.get("coverage")
        if (
            not isinstance(coverage, dict)
            or coverage.get("complete") is not (following is None)
            or type(coverage.get("dropped_count")) is not int
            or coverage["dropped_count"] != 0
            or coverage.get("errors") != []
        ):
            raise ArchiveError("structured Outlook response reports incomplete coverage")
        instance = payload.get("source_instance")
        if (
            not isinstance(instance, str)
            or not instance
            or instance.casefold() != settings.account.casefold()
            or (source is not None and source.casefold() != instance.casefold())
        ):
            raise ArchiveError("Outlook source identity changed during pagination")
        source = instance
        client.pause()
        yield payload
        if following is None:
            break
        if following in seen:
            raise ArchiveError("Outlook pagination repeated a cursor")
        seen.add(following)
        cursor = following


def select_folders(client, settings, selected):
    """Resolve explicit IDs or unambiguous folder names, including child folders."""
    aliases = {"inbox": "inbox", "sent": "sent items"}
    pending = [None]
    visited = set()
    known = {}
    while pending:
        parent = pending.pop(0)
        extra = ["--parent_folder_id", parent] if parent else []
        for payload in _pages(client, settings, "list_folders", "folders", extra):
            for folder in payload["folders"]:
                if (
                    not isinstance(folder, dict)
                    or not isinstance(folder.get("folder_id"), str)
                    or not folder["folder_id"]
                    or not isinstance(folder.get("display_name"), str)
                ):
                    raise ArchiveError("Outlook folder record is invalid")
                children = folder.get("child_folder_count")
                if type(children) is not int or children < 0:
                    raise ArchiveError("Outlook folder child count is invalid")
                identity = folder["folder_id"]
                if identity in known and known[identity] != folder:
                    raise ArchiveError("Outlook folder identity changed while listing")
                known[identity] = folder
                if children and identity not in visited:
                    visited.add(identity)
                    pending.append(identity)
    resolved = []
    for selector in selected:
        if selector in known:
            matches = [selector]
        else:
            name = aliases.get(selector.casefold(), selector.casefold())
            matches = [
                identity
                for identity, folder in known.items()
                if folder["display_name"].casefold() == name
            ]
        if len(matches) != 1:
            raise ArchiveError(
                "selected Outlook folder is missing or ambiguous; configure its folder ID"
            )
        if matches[0] not in resolved:
            resolved.append(matches[0])
    return resolved


def _recipient(value):
    if value is None:
        return ""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("address"), str)
        or not isinstance(value.get("name", ""), str)
    ):
        raise ArchiveError("Outlook recipient record is invalid")
    name, address = value.get("name", ""), value["address"]
    return f"{name} <{address}>" if name else address


def _recipients(values):
    if not isinstance(values, list):
        raise ArchiveError("Outlook recipient list is invalid")
    return ", ".join(_recipient(value) for value in values)


def normalize_email(record, skip_read=False):
    """Translate the public structured response to the established archive format."""
    if not isinstance(record, dict):
        raise ArchiveError("Outlook email record is invalid")
    identity = record.get("message_id")
    if not isinstance(identity, str) or not identity or any(ord(c) < 32 for c in identity):
        raise ArchiveError("Outlook email identity is invalid")
    for field in ("subject", "body_preview"):
        if not isinstance(record.get(field), str):
            raise ArchiveError("Outlook email text field is invalid")
    if record.get("received_at") is not None and not isinstance(record["received_at"], str):
        raise ArchiveError("Outlook email received date is invalid")
    coverage = record.get("body_coverage")
    if coverage not in {"full", "preview", "missing"} or (coverage != "full" and not skip_read):
        raise ArchiveError("Outlook email body is incomplete; full archive state was not advanced")
    body = record.get("body")
    if coverage == "full" and (
        not isinstance(body, dict)
        or body.get("content_type") not in {"text", "html"}
        or not isinstance(body.get("content"), str)
    ):
        raise ArchiveError("Outlook email full-body record is invalid")
    attachments = record.get("attachments")
    if not isinstance(attachments, list):
        raise ArchiveError("Outlook email attachments are invalid")
    normalized_attachments = []
    for attachment in attachments:
        if (
            not isinstance(attachment, dict)
            or not isinstance(attachment.get("name"), str)
            or not isinstance(attachment.get("content_type"), str)
        ):
            raise ArchiveError("Outlook attachment metadata is invalid")
        size = attachment.get("size_bytes")
        if size is not None and (type(size) is not int or size < 0):
            raise ArchiveError("Outlook attachment size is invalid")
        normalized_attachments.append(
            {"name": attachment["name"], "contentType": attachment["content_type"], "size": size}
        )
    if type(record.get("has_attachments")) is not bool:
        raise ArchiveError("Outlook attachment flag is invalid")
    web_url = record.get("web_url")
    if web_url is not None and not isinstance(web_url, str):
        raise ArchiveError("Outlook web URL is invalid")
    full = None
    if not skip_read:
        full = {
            "html_body" if body["content_type"] == "html" else "text_body": body["content"],
            "attachments": normalized_attachments,
            "web_link": web_url or "",
        }
    return {
        "id": identity,
        "subject": record["subject"],
        "date": record.get("received_at") or "",
        "from": _recipient(record.get("from")),
        "to": _recipients(record.get("to")),
        "cc": _recipients(record.get("cc")),
        "hasAttachment": record["has_attachments"],
        "snippet": record["body_preview"],
        "_full": full,
    }


def list_emails(client, settings, folder, after, before, skip_read=False):
    records = {}
    for payload in _pages(
        client,
        settings,
        "list_emails",
        "emails",
        [
            "--folder_id",
            folder,
            "--received_after",
            after.isoformat(),
            "--received_before",
            before.isoformat(),
        ],
    ):
        if payload.get("folder_id") != folder:
            raise ArchiveError("Outlook email page belongs to a different folder")
        for raw in payload["emails"]:
            incomplete = isinstance(raw, dict) and raw.get("body_coverage") in {
                "preview",
                "missing",
            }
            email = normalize_email(raw, skip_read=skip_read or incomplete)
            email["_body_coverage"] = raw["body_coverage"]
            identity = email["id"]
            if identity in records and records[identity] != email:
                raise ArchiveError("Outlook email identity changed between pages")
            records[identity] = email
    return list(records.values())


def html_to_markdown(html_body: str) -> str:
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = False
    converter.body_width = 0
    converter.ignore_emphasis = False
    converter.protect_links = True
    converter.wrap_links = False
    return re.sub(r"\n{4,}", "\n\n\n", converter.handle(html_body)).strip()


def slugify(text: str, max_len=60) -> str:
    """Preserve the archive's existing Unicode-compatible filename convention."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "", text)
    text = re.sub(r"\s+", "-", text.strip())
    return text[:max_len].rstrip("-") or "untitled"


def format_attachment_links(attachments: list) -> str:
    lines = []
    for attachment in attachments:
        name = attachment.get("name", "unknown")
        url = (
            attachment.get("contentUrl")
            or attachment.get("url")
            or attachment.get("content_url")
            or ""
        )
        content_type = attachment.get("contentType", "")
        size = attachment.get("size", "")
        size_text = f" ({size} bytes)" if size else ""
        if url:
            lines.append(f"- [{name}]({url}){size_text} `{content_type}`")
        else:
            lines.append(f"- {name}{size_text} `{content_type}` *(no download link)*")
    return "\n".join(lines)


def email_to_markdown(email_meta: dict, email_full: dict | None) -> str:
    subject = email_meta.get("subject", "No Subject")
    from_addr = email_meta.get("from", "")
    to_addr = email_meta.get("to", "")
    cc_addr = email_meta.get("cc", "")
    sent_at = email_meta.get("date", "")
    attachments_md = ""
    web_link = ""
    if email_full is None:
        body_md = email_meta.get("snippet") or "*(no body)*"
    else:
        html_body = email_full.get("html_body") or email_full.get("body", "")
        if html_body and "<" in html_body:
            body_md = html_to_markdown(html_body)
        else:
            body_md = email_full.get("text_body") or html_body
        if not body_md:
            body_md = "*(no body)*"
        attachments_md = format_attachment_links(email_full.get("attachments", []))
        web_link = email_full.get("web_link", "")
    lines = [f"# {subject}", "", f"- **From:** {from_addr}", f"- **To:** {to_addr}"]
    if cc_addr:
        lines.append(f"- **CC:** {cc_addr}")
    lines.append(f"- **Date:** {sent_at}")
    if email_meta.get("hasAttachment"):
        lines.append("- **Has Attachments:** yes")
    if web_link:
        lines.append(f"- **Outlook Link:** [Open in Outlook]({web_link})")
    lines.extend([f"- **Email ID:** `{email_meta.get('id', '')}`", "", "---", ""])
    if attachments_md:
        lines.extend(["## Attachments", "", attachments_md, "", "---", ""])
    lines.append(body_md)
    return "\n".join(lines)


def bucket_of(filename: str) -> str:
    return filename[:7] if re.match(r"^\d{4}-\d{2}-\d{2}_", filename) else "undated"


def filename_for(email):
    sent_at = email.get("date", "").strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(sent_at)
        except (ValueError, TypeError):
            pass
    file_date = parsed.strftime("%Y-%m-%d_%H%M") if parsed is not None else "unknown"
    short_hash = hashlib.md5(email["id"].encode()).hexdigest()[:8]
    return f"{file_date}_{slugify(email.get('subject', 'No Subject'), max_len=50)}_{short_hash}.md"


def sweep_flat_files(settings):
    """Move prior flat archive files into month buckets without deleting a conflicting copy."""
    moved = 0
    for source in sorted(settings.output_directory.glob("*.md")):
        if source.name in {"README.md", "PROVENANCE.md"}:
            continue
        source = output_path(settings, source.name)
        destination = output_path(settings, Path(bucket_of(source.name)) / source.name)
        if destination.exists() and destination.read_bytes() != source.read_bytes():
            raise ArchiveError("flat email conflicts with an existing month-bucket copy")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        moved += 1
    return moved


def repair_state(settings, state):
    """Unpublished files must be read again even if a local checkpoint survived."""
    suffixes = set()
    for pattern in ("*.md", "*/*.md"):
        for path in settings.output_directory.glob(pattern):
            selected = output_path(settings, path.relative_to(settings.output_directory))
            if selected.is_file():
                suffixes.add(selected.stem.rsplit("_", 1)[-1])
    result = dict(state)
    result["synced_ids"] = [
        identity
        for identity in state["synced_ids"]
        if hashlib.md5(identity.encode()).hexdigest()[:8] in suffixes
    ]
    return result


def pending_bodies(state):
    pending = state.get("pending_bodies", {})
    if not isinstance(pending, dict):
        raise ArchiveError("email pending-body state is invalid")
    for identity, item in pending.items():
        if (
            not isinstance(identity, str)
            or not identity
            or any(ord(char) < 32 for char in identity)
            or not isinstance(item, dict)
            or item.get("body_coverage") not in {"preview", "missing"}
        ):
            raise ArchiveError("email pending-body state is invalid")
        try:
            date.fromisoformat(item["after"])
        except (KeyError, ValueError, TypeError) as error:
            raise ArchiveError("email pending-body retry date is invalid") from error
    return dict(pending)


def selected_dates(args, settings, state=None):
    today = datetime.now(timezone.utc).date()
    try:
        after = (
            date.fromisoformat(args.after)
            if args.after
            else today - timedelta(days=settings.options.get("lookback_days", 7))
        )
        before = date.fromisoformat(args.before) if args.before else today + timedelta(days=1)
        if not args.after and state:
            if state.get("last_before"):
                after = min(after, date.fromisoformat(state["last_before"]) - timedelta(days=1))
            for item in pending_bodies(state).values():
                after = min(after, date.fromisoformat(item["after"]))
    except ValueError as error:
        raise ArchiveError("email date bounds must use YYYY-MM-DD") from error
    if after >= before:
        raise ArchiveError("email start date must precede its end date")
    return after, before


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    parser.add_argument(
        "--after", help="start date YYYY-MM-DD; default is seven days before today in UTC"
    )
    parser.add_argument("--before", help="end date YYYY-MM-DD; default is tomorrow in UTC")
    parser.add_argument(
        "--folders", help="comma-separated folders; default comes from private configuration"
    )
    parser.add_argument(
        "--skip-read",
        action="store_true",
        help="explicitly write list snippets; these IDs remain eligible for a later full read",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_config(args.config, "emails", root=args.root, state_file=args.state_file)
        state = read_state(settings.state_file)
        pending = pending_bodies(state)
        after, before = selected_dates(args, settings, state)
        folders = (
            [part.strip() for part in args.folders.split(",")]
            if args.folders is not None
            else settings.options.get("folders", ["inbox", "sent"])
        )
        if not folders or any(
            not folder or folder.startswith("-") or any(ord(c) < 32 for c in folder)
            for folder in folders
        ):
            raise ArchiveError("email folder selection is invalid")
        folders = list(dict.fromkeys(folders))
        if args.doctor:
            if not shutil.which(settings.command[0]):
                raise ArchiveError("configured archive command is not installed")
            print("OK email archive configuration and command are available")
            return 0
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "after": after.isoformat(),
                        "before": before.isoformat(),
                        "folders": folders,
                        "skip_read": args.skip_read,
                        "page_size": settings.options.get("page_size", 50),
                    }
                )
            )
            return 0
        state = repair_state(settings, state)
        synced = set(state["synced_ids"])
        client = Client(settings)
        unique = {}
        for folder in select_folders(client, settings, folders):
            for email in list_emails(
                client, settings, folder, after, before, skip_read=args.skip_read
            ):
                unique[email["id"]] = email
        selected = sorted(unique.values(), key=lambda item: item.get("date", ""), reverse=True)
        moved = sweep_flat_files(settings)
        written = 0
        for email in selected:
            identity = email["id"]
            if identity in synced:
                pending.pop(identity, None)
                continue
            if not args.skip_read and email["_full"] is None:
                previous = pending.get(identity, {})
                pending[identity] = {
                    "after": min(previous.get("after", after.isoformat()), after.isoformat()),
                    "body_coverage": email["_body_coverage"],
                }
                continue
            filename = filename_for(email)
            destination = output_path(settings, Path(bucket_of(filename)) / filename)
            full = None if args.skip_read else email["_full"]
            write_text(destination, email_to_markdown(email, full))
            if not args.skip_read:
                synced.add(identity)
                pending.pop(identity, None)
            written += 1
        state.update(synced_ids=sorted(synced), pending_bodies=pending)
        if not pending and not args.skip_read:
            state.update(
                last_sync=datetime.now(timezone.utc).isoformat(),
                last_after=after.isoformat(),
                last_before=before.isoformat(),
            )
        write_state(settings.state_file, state)
        for identity, item in sorted(pending.items()):
            print(
                f"WARN email body {item['body_coverage']}; message_id={json.dumps(identity)}; "
                f"retry from {item['after']}; not marked complete",
                file=sys.stderr,
            )
        status = "WARN" if pending or args.skip_read else "OK"
        print(
            f"{status} email archive: {len(selected)} selected, {written} written, "
            f"{moved} moved to month directories, {len(synced)} full messages checkpointed, "
            f"{len(pending)} bodies pending"
        )
        return 0
    except ArchiveError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, OverflowError):
        print("FAIL email archive operation could not complete", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
