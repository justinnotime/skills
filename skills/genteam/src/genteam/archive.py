"""Mechanically archive selected GenTeam channels and threads. Zero model calls."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import APIError, AuthExpired, Client, channel_label, fingerprint, rows, slugify
from .config import ConfigurationError, Settings

SETTINGS: Settings | None = None
CLIENT: Client | None = None
OUTPUT_DIR: Path | None = None
STATE_FILE: Path | None = None
PAGE_SIZE = 200  # API max per channel_messages contract
RATE_DELAY = 0.5  # polite pacing between requests
MAX_PAGES_PER_RUN = 40  # per channel per run; backlog continues next tick
DEFAULT_BOOTSTRAP_DAYS = 90


def log(msg: str):
    print(msg, flush=True)


def warn(msg: str):
    print(f"  [WARN] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# HTTP plumbing (cookie never printed; fingerprint only)
# ---------------------------------------------------------------------------


def cookie() -> str:
    return CLIENT.cookie()


def cookie_fingerprint() -> str:
    return fingerprint(cookie())


def api_get(path: str, params: dict | None = None) -> dict:
    return CLIENT.request("GET", path, params=params)


def load_config() -> dict:
    cfg = dict(SETTINGS.get("archive.selection", {}))
    cfg.setdefault("enabled", False)
    cfg.setdefault("mode", "whitelist")
    cfg.setdefault("chats", [])
    cfg.setdefault("bootstrap_days", DEFAULT_BOOTSTRAP_DAYS)
    cfg.setdefault("threads", False)
    if cfg["mode"] not in {"whitelist", "blacklist", "pinned"}:
        raise ConfigurationError("archive selection mode must be whitelist, blacklist or pinned")
    if not isinstance(cfg["chats"], list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("match"), str) or not entry["match"]
        for entry in cfg["chats"]
    ):
        raise ConfigurationError("archive selection entries require a nonempty match")
    if not isinstance(cfg["bootstrap_days"], int) or cfg["bootstrap_days"] <= 0:
        raise ConfigurationError("bootstrap_days must be a positive integer")
    return cfg


def selected(label: str, ch: dict, cfg: dict) -> tuple[bool, str]:
    """Apply the configured selection. Returns (selected, alias)."""
    if cfg["mode"] == "pinned":
        return ch.get("pinned") is True, label
    hay = label.lower()
    for entry in cfg["chats"]:
        if entry["match"].lower() in hay:
            if cfg["mode"] == "whitelist":
                return True, entry.get("alias") or label
            return False, label
    return (cfg["mode"] == "blacklist"), label


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            warn("state file corrupt; starting fresh (dedup by message id)")
    return {"channels": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Rendering (verbatim; mechanical transforms only)
# ---------------------------------------------------------------------------


def render_message(item: dict) -> str | None:
    d = item.get("data") or {}
    ts = item.get("ts") or ""
    kind = item.get("kind") or d.get("kind") or ""
    if kind == "system_message":
        text = (d.get("display_text") or d.get("content") or "").strip()
        if not text:
            return None
        return f"- `{ts}` *(system)* {text}\n"
    sender = d.get("sender_display_name") or d.get("sender_actor_id") or "unknown"
    actor_type = d.get("sender_actor_type") or ""
    tag = " [agent]" if actor_type == "agent" else ""
    body = (d.get("content") or d.get("display_text") or "").rstrip()
    lines = [f"### `{ts}` {sender}{tag}\n"]
    if d.get("parent_comet_message_id"):
        lines.append(f"*reply to message {d['parent_comet_message_id']}*\n")
    lines.append(body + "\n")
    for att in d.get("attachments") or []:
        name = att.get("name") or att.get("file_name") or "attachment"
        url = att.get("url") or att.get("file_url") or ""
        lines.append(f"- attachment: {name} {url}\n".rstrip() + "\n")
    reactions = d.get("reactions") or []
    if reactions:
        compact = ", ".join(
            f"{r.get('reaction') or r.get('emoji')} x{r.get('count', 1)}"
            for r in reactions
            if isinstance(r, dict)
        )
        if compact:
            lines.append(f"*reactions: {compact}*\n")
    return "\n".join(lines)


def month_of(ts: str) -> str:
    value = (ts or "")[:7]
    return value if re.fullmatch(r"[0-9]{4}-[0-9]{2}", value) else "unknown"


def append_to_month(
    channel_dir: Path, alias: str, server_slug: str, channel_id: str, items: list[dict]
):
    """Append rendered messages to their YYYY-MM.md buckets, oldest first."""
    by_month: dict[str, list[tuple[str, str]]] = {}
    for item in items:
        rendered = render_message(item)
        if rendered:
            mid = str((item.get("data") or {}).get("comet_message_id") or "")
            marker = f"<!-- genteam-message: {mid} -->"
            by_month.setdefault(month_of(item.get("ts")), []).append((marker, rendered))
    appended = 0
    for month, blocks in sorted(by_month.items()):
        f = channel_dir / f"{month}.md"
        if not f.exists():
            channel_dir.mkdir(parents=True, exist_ok=True)
            f.write_text(
                "---\n"
                f"chat: {json.dumps(alias, ensure_ascii=False)}\n"
                f"server: {json.dumps(server_slug, ensure_ascii=False)}\n"
                f"channel_id: {json.dumps(channel_id)}\n"
                f'month: "{month}"\n'
                "source: genteam REST API (digital-employee), session-cookie auth\n"
                "extraction: mechanical (verbatim bodies; no LLM)\n"
                "---\n\n"
                f"# {alias} — {month}\n\n"
            )
        existing = f.read_text()
        with f.open("a") as fh:
            for marker, block in blocks:
                if marker in existing:
                    continue
                fh.write(marker + "\n" + block + "\n")
                appended += 1
    return appended


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_channel(ch: dict, alias: str, server_slug: str, state: dict, bootstrap_days: int) -> int:
    """Sync one channel; returns number of NEW rendered messages."""
    cid = ch["id"]
    cstate = state["channels"].setdefault(cid, {})
    newest_synced = cstate.get("newest_id")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=bootstrap_days)).isoformat()
    collected: list[dict] = []

    if newest_synced and not cstate.get("bootstrap_before_id"):
        # Incremental: page forward from the watermark.
        cursor = str(newest_synced)
        for _ in range(MAX_PAGES_PER_RUN):
            page = api_get(
                f"/channels/{cid}/messages", {"limit": PAGE_SIZE, "after_message_id": cursor}
            )
            items = rows(page, "items")
            collected.extend(
                item
                for item in items
                if int((item.get("data") or {}).get("comet_message_id") or 0) > int(newest_synced)
            )
            if not page.get("has_more_newer"):
                break
            next_cursor = page.get("newest_comet_message_id")
            if not next_cursor or str(next_cursor) == str(cursor):
                raise APIError("incremental page claims more messages but has no advancing cursor")
            cursor = next_cursor
            time.sleep(RATE_DELAY)
    else:
        # Bootstrap: newest page, then walk backward until the cutoff.
        pages: list[list[dict]] = []
        cursor = cstate.get("bootstrap_before_id", "")
        cutoff = cstate.get("bootstrap_cutoff", cutoff)
        exhausted = True
        for _ in range(MAX_PAGES_PER_RUN):
            params = {"limit": PAGE_SIZE}
            if cursor:
                params["before_message_id"] = cursor
            page = api_get(f"/channels/{cid}/messages", params)
            items = rows(page, "items")
            if not items:
                exhausted = False
                break
            pages.append(items)
            oldest_ts = items[0].get("ts") or ""
            if not page.get("has_more") or (oldest_ts and oldest_ts < cutoff):
                exhausted = False
                break
            cursor = page.get("oldest_comet_message_id") or ""
            if not cursor:
                raise APIError("bootstrap page claims more messages but has no cursor")
            time.sleep(RATE_DELAY)
        if exhausted and cursor:
            cstate["bootstrap_before_id"] = cursor
            cstate["bootstrap_cutoff"] = cutoff
        else:
            cstate.pop("bootstrap_before_id", None)
            cstate.pop("bootstrap_cutoff", None)
        for items in reversed(pages):
            collected.extend(i for i in items if (i.get("ts") or "") >= cutoff)

    if not collected:
        return 0
    # Ordered oldest->newest by server int id; dedupe defensively.
    seen: set[str] = set()
    ordered = []
    for item in collected:
        mid = str((item.get("data") or {}).get("comet_message_id") or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        ordered.append(item)
    ordered.sort(key=lambda i: int((i.get("data") or {}).get("comet_message_id") or 0))
    if not ordered:
        return 0

    # alias may be a path ("<channel>/threads/<short-id>"); slug each segment.
    channel_dir = OUTPUT_DIR / slugify(server_slug)
    for part in alias.split("/"):
        channel_dir = channel_dir / slugify(part)
    appended = append_to_month(channel_dir, alias, server_slug, cid, ordered)
    cstate["newest_id"] = str(
        max(int(newest_synced or 0), int((ordered[-1].get("data") or {}).get("comet_message_id")))
    )
    cstate["alias"] = alias
    cstate["last_sync"] = datetime.now(timezone.utc).isoformat()
    return appended


def list_threads(channel_id: str) -> list[dict]:
    """Thread channel rows of a channel (reply_count, last_reply_at, ids)."""
    return rows(api_get(f"/channels/{channel_id}/threads"), "threads")


def fetch_channel_threads(
    ch: dict, alias: str, server_slug: str, state: dict, bootstrap_days: int
) -> int:
    """Mirror every thread of a channel (config threads: true). Each thread
    is itself a channel, so it reuses fetch_channel's watermark mechanics;
    output lands under <channel-slug>/threads/<thread-short-id>/."""
    total = 0
    for t in list_threads(ch["id"]):
        if not t.get("id"):
            continue
        short = t.get("thread_short_id") or t["id"][-8:]
        thread_alias = f"{alias}/threads/{short}"
        try:
            total += fetch_channel(t, thread_alias, server_slug, state, bootstrap_days)
        except AuthExpired:
            raise
        except (APIError, OSError, ValueError, KeyError, TypeError) as e:
            raise APIError(f"thread {thread_alias} failed; progress not committed") from e
        time.sleep(RATE_DELAY)
    return total


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def visible_channels(*, include_pins=False) -> list[tuple[dict, dict, str]]:
    return [
        (channel, members, slug)
        for channel, members, _sid, slug in CLIENT.channels(include_pins=include_pins)
    ]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path)
    ap.add_argument(
        "--publish", action="store_true", help="run through the configured external publisher"
    )
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--state-file", type=Path)
    ap.add_argument("--list-channels", action="store_true")
    ap.add_argument("--list-selected", action="store_true", help="list the current archive selection")
    ap.add_argument("--peek", metavar="MATCH")
    ap.add_argument("--peek-limit", type=int, default=20)
    ap.add_argument("--threads", metavar="MATCH", help="list an existing channel's threads")
    ap.add_argument(
        "--peek-thread", metavar="THREAD_ID", help="print a thread's messages (read-only)"
    )
    args = ap.parse_args(argv)
    configure(args)
    if args.publish:
        return publish(args)

    try:
        cookie()
    except AuthExpired:
        if SETTINGS.get("archive.missing_cookie", "fail") == "skip":
            warn("GenTeam cookie unavailable; configured skip")
            return 0
        raise

    if args.list_channels or args.list_selected:
        cfg = load_config() if args.list_selected else None
        if cfg is not None and not cfg["enabled"]:
            return 0
        for ch, members, slug in visible_channels(
            include_pins=cfg is not None and cfg["mode"] == "pinned"
        ):
            label = channel_label(ch, members)
            if cfg is not None and not selected(label, ch, cfg)[0]:
                continue
            print(f"{ch['id']}  [{ch.get('channel_type')}]  {slug}/{label}")
        return

    if args.peek:
        needle = args.peek.lower()
        for ch, members, slug in visible_channels():
            label = channel_label(ch, members)
            if needle in label.lower():
                page = api_get(
                    f"/channels/{ch['id']}/messages", {"limit": min(args.peek_limit, PAGE_SIZE)}
                )
                for item in rows(page, "items"):
                    r = render_message(item)
                    if r:
                        print(r)
                return
        warn(f"no visible channel matches {args.peek!r}")
        sys.exit(1)

    if args.threads:
        needle = args.threads.lower()
        for ch, members, slug in visible_channels():
            label = channel_label(ch, members)
            if needle in label.lower():
                threads = list_threads(ch["id"])
                if not threads:
                    print(f"no threads in {slug}/{label}")
                    return
                for t in threads:
                    print(
                        f"{t.get('id')}  replies={t.get('reply_count')}  "
                        f"last={t.get('last_reply_at')}  "
                        f"short={t.get('thread_short_id')}"
                    )
                return
        warn(f"no visible channel matches {args.threads!r}")
        sys.exit(1)

    if args.peek_thread:
        page = api_get(
            f"/channels/{args.peek_thread}/messages", {"limit": min(args.peek_limit, PAGE_SIZE)}
        )
        for item in rows(page, "items"):
            r = render_message(item)
            if r:
                print(r)
        return

    cfg = load_config()
    if not cfg["enabled"]:
        log("genteam: disabled in configuration")
        return 0
    if OUTPUT_DIR is None or STATE_FILE is None:
        raise ConfigurationError("archive output_directory and state_file are required")
    state = load_state()
    total = 0
    chans = visible_channels(include_pins=cfg["mode"] == "pinned")
    log(f"genteam: {len(chans)} channels visible (cookie fingerprint {cookie_fingerprint()})")
    picked = 0
    failed = False
    for ch, members, slug in chans:
        label = channel_label(ch, members)
        sel, alias = selected(label, ch, cfg)
        if not sel:
            continue
        picked += 1
        try:
            n = fetch_channel(ch, alias, slug, state, cfg["bootstrap_days"])
        except AuthExpired:
            raise
        except (APIError, OSError, ValueError, KeyError, TypeError) as e:
            warn(f"channel {label}: {e}; progress not committed")
            failed = True
            continue
        if n:
            log(f"  {slug}/{label}: +{n} messages")
            total += n
        if cfg["threads"]:
            tn = fetch_channel_threads(ch, alias, slug, state, cfg["bootstrap_days"])
            if tn:
                log(f"  {slug}/{label}: +{tn} thread messages")
                total += tn
        time.sleep(RATE_DELAY)
    if failed:
        raise APIError("selected channels failed; progress was not advanced")
    save_state(state)
    log(f"genteam: done, {picked} channels selected, {total} new messages")


def configure(args):
    global SETTINGS, CLIENT, OUTPUT_DIR, STATE_FILE, RATE_DELAY, MAX_PAGES_PER_RUN
    SETTINGS = Settings(args.config)
    CLIENT = Client(SETTINGS)
    OUTPUT_DIR = args.output_dir or SETTINGS.path("archive.output_directory")
    STATE_FILE = args.state_file or SETTINGS.path("archive.state_file")
    worktree = os.environ.get("REPOSITORY_PUBLISH_WORKTREE")
    if worktree:
        relative = Path(str(SETTINGS.get("archive.repository_path", "")))
        if str(relative) in {"", "."} or relative.is_absolute() or ".." in relative.parts:
            raise ConfigurationError("archive.repository_path must be a relative owned subtree")
        OUTPUT_DIR = Path(worktree) / relative
    state = os.environ.get("REPOSITORY_PUBLISH_STATE") or os.environ.get("SYNC_STATE_DIR")
    if state:
        STATE_FILE = Path(state) / (STATE_FILE.name if STATE_FILE else "genteam.state.json")
    RATE_DELAY = float(SETTINGS.get("archive.rate_delay", 0.5))
    MAX_PAGES_PER_RUN = int(SETTINGS.get("archive.max_pages_per_run", 40))
    if RATE_DELAY < 0 or MAX_PAGES_PER_RUN <= 0:
        raise ConfigurationError("archive rate_delay/max_pages_per_run are invalid")


def publish(args):
    if args.list_channels or args.list_selected or args.peek or args.threads or args.peek_thread:
        raise ConfigurationError("--publish cannot be combined with read-only discovery")
    command = SETTINGS.command("publisher.command")
    if not command:
        raise ConfigurationError("publisher.command is required for --publish")
    writer = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts/sync"),
        "--config",
        str(SETTINGS.source),
    ]
    if "{command}" in command:
        expanded = []
        for arg in command:
            expanded.extend(writer if arg == "{command}" else [arg])
        command = expanded
    else:
        command += ([] if command[-1] == "--" else ["--"]) + writer
    return subprocess.run(command, check=False).returncode


def main(argv=None):
    try:
        return run(argv) or 0
    except (APIError, ConfigurationError, OSError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
