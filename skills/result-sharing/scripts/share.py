#!/usr/bin/env python3
"""Publish explicit result bundles to a configured local or SSH destination."""

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit

SCHEMA = "result-sharing/v1"
MAX_BYTES = 100 * 1024 * 1024
MAX_FILES = 2000
ASSETS = Path(__file__).resolve().parents[1] / "assets"


def fail(message):
    raise ValueError(message)


def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            fail("Duplicate JSON key")
        value[key] = item
    return value


def decode(data):
    return json.loads(data, object_pairs_hook=pairs)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def path(value):
    p = Path(value).expanduser().absolute()
    if p.resolve() != p:
        fail("Symlink or noncanonical path rejected")
    return p


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        fail("Invalid bundle path")
    p = PurePosixPath(value)
    if (
        p.is_absolute()
        or str(p) != value
        or any(x in (".", "..", ".git", ".ssh") or x.startswith(".") for x in p.parts)
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        fail("Unsafe bundle path")
    return value


def web_url(value, optional=False):
    if optional and not value:
        return ""
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or not u.netloc or u.username or u.password:
        fail("Expected an HTTP(S) URL without credentials")
    return value


def config(filename):
    value = decode(Path(filename).expanduser().read_bytes())
    if value.get("schema") != SCHEMA:
        fail("Unsupported configuration schema")
    return value


def limits(cfg):
    count, size = cfg.get("max_files", MAX_FILES), cfg.get("max_bytes", MAX_BYTES)
    if (
        not isinstance(count, int)
        or not isinstance(size, int)
        or not (0 < count <= MAX_FILES and 0 < size <= MAX_BYTES)
    ):
        fail("Invalid limits")
    return count, size


def bundle(cfg, source, project, title, entry, files, summary="", conversation_url=""):
    source = path(source)
    roots = [path(x) for x in cfg.get("allowed_source_roots", [])]
    if not roots or not any(source.is_relative_to(root) for root in roots):
        fail("Source is outside configured publication roots")
    count, size = limits(cfg)
    if not files or len(files) > count or len(set(files)) != len(files):
        fail("Invalid file selection")
    payload = {}
    total = 0
    for name in files:
        relative(name)
        p = path(source / name)
        if not p.is_file() or not p.is_relative_to(source):
            fail("Selected file is not regular or escapes source")
        total += p.stat().st_size
        if total > size:
            fail("Selected files exceed byte limit")
        data = p.read_bytes()
        payload[name] = {"sha256": sha(data), "data": base64.b64encode(data).decode()}
    value = {
        "schema": SCHEMA,
        "project": project,
        "title": title,
        "summary": summary,
        "entry": entry,
        "conversation_url": conversation_url,
        "files": payload,
    }
    validate(cfg, value)
    return value


def validate(cfg, value):
    if value.get("schema") != SCHEMA:
        fail("Unsupported bundle schema")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", value.get("project", "")):
        fail("Invalid project slug")
    allowed = cfg.get("allowed_projects")
    if allowed is not None and value["project"] not in allowed:
        fail("Project is not authorized by receiver")
    for name, bound in (("title", 200), ("summary", 4000)):
        if not isinstance(value.get(name), str) or len(value[name]) > bound:
            fail("Invalid result metadata")
    if not value["title"].strip():
        fail("A title is required")
    web_url(value.get("conversation_url", ""), optional=True)
    count, size = limits(cfg)
    files = value.get("files")
    if not isinstance(files, dict) or not files or len(files) > count:
        fail("Invalid file selection")
    decoded, total = {}, 0
    for name, item in files.items():
        relative(name)
        data = base64.b64decode(item["data"], validate=True)
        total += len(data)
        if total > size or sha(data) != item["sha256"]:
            fail("Bundle size or checksum mismatch")
        decoded[name] = data
    if relative(value["entry"]) not in files:
        fail("Entry file is missing")
    return decoded


def page(title, body):
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + html.escape(title) + "</title><style>"
        "body{font:17px/1.65 system-ui;max-width:960px;margin:4vh auto;padding:0 20px;"
        "background:#121820;color:#e5ebf1;overflow-wrap:anywhere}a{color:#8dceff}"
        "article{border:1px solid #34414e;padding:20px;border-radius:12px;margin:16px 0}"
        "p{white-space:pre-wrap}small{color:#a0aeba}input{font:inherit;padding:10px;"
        "width:100%;box-sizing:border-box;background:#1b2530;color:inherit;border:1px solid #567}"
        "</style><h1>" + html.escape(title) + "</h1>" + body + "</html>"
    ).encode()


def atomic(p, data):
    with tempfile.NamedTemporaryFile(
        dir=p.parent, prefix=".write-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, p)
    finally:
        temporary.unlink(missing_ok=True)


def catalog(root):
    projects = root / "projects"
    rows = []
    library = []
    for project in sorted(projects.iterdir()) if projects.exists() else []:
        if not project.is_dir() or project.is_symlink():
            continue
        releases = []
        for item in project.iterdir():
            if not re.fullmatch(r"[a-f0-9]{64}", item.name) or item.is_symlink():
                continue
            metadata = path(item / "manifest.json")
            releases.append(decode(metadata.read_bytes()))
        releases.sort(key=lambda x: (x["published_at"], x["revision"]), reverse=True)
        if not releases:
            continue
        cards = []
        for r in releases:
            cards.append(
                '<article><a href="'
                + r["revision"]
                + '/">'
                + html.escape(r["title"])
                + "</a><p>"
                + html.escape(r["summary"])
                + "</p><small>"
                + r["published_at"]
                + "</small></article>"
            )
        atomic(
            project / "index.html",
            page(
                releases[0]["title"],
                '<a href="../../">All results</a><p><a href="'
                + releases[0]["revision"]
                + "/files/"
                + quote(releases[0]["entry"], safe="/")
                + '">Open latest / 打开最新版本</a></p>'
                + "".join(cards),
            ),
        )
        latest = releases[0]
        rows.append(latest)
        library.append({"project": latest["project"], "releases": releases})
    rows.sort(key=lambda x: (x["published_at"], x["project"]), reverse=True)
    library.sort(
        key=lambda x: (x["releases"][0]["published_at"], x["project"]), reverse=True
    )
    atomic(
        root / "catalog.json", canonical({"schema": SCHEMA, "projects": rows}) + b"\n"
    )
    atomic(
        root / "library.json",
        canonical({"schema": SCHEMA, "projects": library}) + b"\n",
    )
    for name in ("library.css", "library.js"):
        atomic(root / name, (ASSETS / name).read_bytes())
    atomic(root / "index.html", (ASSETS / "library.html").read_bytes())


def receive(cfg, value):
    decoded = validate(
        cfg, value
    )  # No destination mutations until validation completes.
    if cfg.get("transport", {}).get("kind", "local") != "local":
        fail("Receiver must have a local destination")
    root = path(cfg["root"])
    url = web_url(cfg["base_url"]).rstrip("/")
    content = {
        k: value[k]
        for k in ("schema", "project", "title", "summary", "entry", "conversation_url")
    }
    content["files"] = {
        k: {"sha256": sha(v), "bytes": len(v)} for k, v in decoded.items()
    }
    revision = sha(canonical(content))
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Root must be a dedicated publication directory, never a repository root.
    path(root / ".publish.lock")
    with (root / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        project = path(root / "projects" / value["project"])
        project.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = path(project / revision)
        if destination.exists():
            existing = decode(path(destination / "manifest.json").read_bytes())
            if any(existing.get(k) != v for k, v in content.items()):
                fail("Existing immutable release metadata differs")
            for name, data in decoded.items():
                if sha(path(destination / "files" / name).read_bytes()) != sha(data):
                    fail("Existing immutable release was modified")
        else:
            staging = Path(tempfile.mkdtemp(prefix=".publish-", dir=project))
            try:
                for name, data in decoded.items():
                    target = staging / "files" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                content.update(
                    revision=revision,
                    published_at=datetime.now(timezone.utc).isoformat(
                        timespec="microseconds"
                    ),
                )
                (staging / "manifest.json").write_bytes(canonical(content) + b"\n")
                body = (
                    '<a href="../">Project history</a> · <a href="../../../">All results</a><p>'
                    + html.escape(value["summary"])
                    + "</p>"
                )
                body += (
                    '<p><a href="files/'
                    + quote(value["entry"], safe="/")
                    + '">Open result / 打开产物</a></p>'
                )
                if value["conversation_url"]:
                    body += (
                        '<p><a rel="noopener noreferrer" href="'
                        + html.escape(value["conversation_url"], quote=True)
                        + '">Related conversation / 相关对话</a></p>'
                    )
                body += (
                    "<ul>"
                    + "".join(
                        '<li><a href="files/'
                        + quote(name, safe="/")
                        + '">'
                        + html.escape(name)
                        + "</a></li>"
                        for name in sorted(decoded)
                    )
                    + "</ul>"
                )
                (staging / "index.html").write_bytes(page(value["title"], body))
                staging.rename(destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        catalog(
            root
        )  # Re-running an identical publish also repairs catalog interruption.
    prefix = url + "/projects/" + value["project"]
    return {
        "schema": SCHEMA,
        "project_url": prefix + "/",
        "result_url": prefix + "/" + revision + "/",
        "entry_url": prefix
        + "/"
        + revision
        + "/files/"
        + quote(value["entry"], safe="/"),
        "revision": revision,
    }


def publish(cfg, value):
    transport = cfg.get("transport", {"kind": "local"})
    if transport["kind"] == "local":
        return receive(cfg, value)
    if transport["kind"] != "ssh":
        fail("Unknown transport")
    target = transport["target"]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", target):
        fail("Use a configured SSH host alias")
    command = transport["receiver_command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(x, str) and x for x in command)
    ):
        fail("Receiver command must be an argument array")
    result = subprocess.run(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "--",
            target,
            shlex.join(command),
        ],
        input=canonical(value),
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        fail("SSH receiver failed; bundle remains local and no success URL is claimed")
    reply = decode(result.stdout)
    if reply.get("schema") != SCHEMA or not re.fullmatch(
        r"[a-f0-9]{64}", reply.get("revision", "")
    ):
        fail("Invalid receiver receipt")
    for name in ("result_url", "project_url", "entry_url"):
        web_url(reply[name])
    return reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "RESULT_SHARING_CONFIG", "~/.config/result-sharing/config.json"
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--source", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--summary", default="")
    p.add_argument("--conversation-url", default="")
    p.add_argument("--entry", required=True)
    p.add_argument("--files", nargs="+", required=True)
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("receive")
    sub.add_parser("reindex")
    args = parser.parse_args()
    try:
        cfg = config(args.config)
        if args.action == "publish":
            value = bundle(
                cfg,
                args.source,
                args.project,
                args.title,
                args.entry,
                args.files,
                args.summary,
                args.conversation_url,
            )
            output = (
                {"validated_files": len(value["files"]), "published": False}
                if args.dry_run
                else publish(cfg, value)
            )
        elif args.action == "receive":
            _, size = limits(cfg)
            bound = size * 2 + 1024 * 1024
            raw = sys.stdin.buffer.read(bound + 1)
            if len(raw) > bound:
                fail("Input exceeds receiver limit")
            output = receive(cfg, decode(raw))
        else:
            root = path(cfg["root"])
            with path(root / ".publish.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                catalog(root)
            output = {"reindexed": True}
        print(json.dumps(output, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        print(
            "Publication failed: check configuration, selected files and destination; no success is claimed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
