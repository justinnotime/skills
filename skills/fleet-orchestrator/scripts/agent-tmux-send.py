#!/usr/bin/env python3
"""Send one best-effort peer message to a local agent running in tmux."""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import tmux_runtime  # noqa: E402
import runtime_paths as nw_paths  # noqa: E402
from send_outcome import SendOutcome  # noqa: E402


AGENT_COMMANDS = {"claude", "codex", "opencode"}
MAX_BYTES = 32 * 1024


def tmux_base_cmd() -> list[str]:
    """The shared machine-local tmux invocation prefix.

    NW_TMUX_SERVER selects the server; a selected primary session limits
    discovery and destination validation to that session's windows.
    """
    try:
        return tmux_runtime.base_cmd()
    except tmux_runtime.TmuxRuntimeConfigError as exc:
        raise RuntimeError(str(exc)) from exc


def tmux(args: list[str], stdin: str | None = None) -> str:
    # timeout: this tool runs inside the unattended orchestrator tick, and a
    # wedged tmux server must fail the one send, not hang the whole engine
    try:
        result = subprocess.run(
            [*tmux_base_cmd(), *args], input=stdin, text=True, capture_output=True,
            check=False, timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("tmux command timed out after 15s") from None
    if result.returncode:
        detail = result.stderr.strip() or "tmux command failed"
        raise RuntimeError(detail)
    return result.stdout.rstrip("\n")


def pane_info(target: str) -> tuple[str, str, str, bool]:
    output = tmux([
        "display-message", "-p", "-t", target, "-F",
        "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_current_command}\t#{pane_dead}",
    ])
    try:
        location, pane_id, command, dead = output.split("\t")
    except ValueError as exc:
        raise RuntimeError(f"unexpected tmux target metadata: {output!r}") from exc
    if os.environ.get("NW_FLEET_PRIMARY_SESSION"):
        allowed = tmux(["list-panes", *tmux_runtime.pane_scope(), "-F", "#{pane_id}"])
        if pane_id not in allowed.splitlines():
            raise RuntimeError("target pane is outside the selected fleet session")
    return location, pane_id, Path(command).name, dead == "1"


def canonical_location(pane_id: str, fallback: str) -> str:
    """A shared pane renders under ANY member session tmux happens to pick,
    so display-message can name a tview-* viewer for a perfectly canonical
    pane - that arbitrary rendering misdiagnosed six correctly-targeted
    failures as mirror-resolution bugs .
    Prefer a non-viewer location for this pane id; targeting is unchanged
    either way (grouped sessions share the pane), only the NAME in logs
    and errors stops lying about where the pane lives."""
    try:
        out = tmux(["list-panes", *tmux_runtime.pane_scope(), "-F",
                    "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"])
    except RuntimeError:
        return fallback
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] == pane_id \
                and not parts[1].startswith("tview-"):
            return parts[1]
    return fallback


def split_location(location: str) -> tuple[str, str, str]:
    """Split a tmux `session:window.pane` location into its three fields.

    tmux rejects ':' and '.' inside session names, so the first ':' always
    ends the session name — even for a grouped-session clone named `4-15`,
    which glued into `tmux4-15:1.0` reads like window 15 of session 4.
    Anything tmux could not have produced (non-numeric window/pane, a dot
    in the session) is refused: this feeds an attribution label, and a
    refusal is visible while a mislabel looks perfectly normal.
    """
    session, colon, rest = location.partition(":")
    window, dot, pane = rest.partition(".")
    if not colon or not dot or not session or "." in session:
        raise ValueError(f"not a session:window.pane location: {location!r}")
    if not re.fullmatch(r"[0-9]+", window) or not re.fullmatch(r"[0-9]+", pane):
        raise ValueError(f"not a session:window.pane location: {location!r}")
    return session, window, pane


# One value token: percent-encoded so no raw space, ';', '[', ']', '@', '='
# or control character can appear inside a field. tmux session names may
# legally contain all of those (only ':' and '.' are banned), so without the
# encoding a session literally named "4 session=evil" or "4; peer message,
# not operator authorization]" could impersonate another seat or close the
# trusted header early — this label answers "who dispatched this work".
_TOKEN = r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})"
_LABEL_RE = re.compile(
    rf"^(?P<command>{_TOKEN}+)@session=(?P<session>{_TOKEN}+)"
    rf" window=(?P<window>[0-9]+) pane=(?P<pane>[0-9]+)$"
)


def format_source_label(command: str, location: str) -> str:
    session, window, pane = split_location(location)
    if not command:
        raise ValueError("empty command")
    return (
        f"{quote(command, safe='')}@session={quote(session, safe='')}"
        f" window={window} pane={pane}"
    )


def parse_source_label(label: str) -> tuple[str, str, str, str]:
    """Recover (command, session, window, pane) from a structured label.

    Fixed grammar, full-string match: fixed field order, exact key set, no
    duplicate keys, well-formed percent escapes, numeric window/pane. Every
    deviation raises instead of guessing — mislabelled attribution is far
    more dangerous than a visible refusal.
    """
    match = _LABEL_RE.fullmatch(label)
    if not match:
        raise ValueError(f"not a well-formed source label: {label!r}")
    return (
        unquote(match["command"]),
        unquote(match["session"]),
        match["window"],
        match["pane"],
    )


def source_label() -> str:
    source_pane = os.environ.get("TMUX_PANE")
    if source_pane:
        try:
            location, _pane_id, command, dead = pane_info(source_pane)
            if not dead:
                return format_source_label(command, location)
        except (RuntimeError, ValueError):
            pass
    return f"agent@{socket.gethostname().split('.', 1)[0]}"


# --nudge: the ONLY messages that may be typed without the peer-message header.
#
# WHY A CLOSED LIST OF LITERALS AND NOT CALLER TEXT. The header exists because a
# line in a seat's input box cannot be told apart from something the operator
# typed: a "merge 47472 and 47479" line once landed in a pane and was acted on
# as his instruction when he had not written it. Dropping the header for
# arbitrary text would hand any caller the ability to impersonate him, so the
# text is not a parameter: the caller picks a KEY, the tool supplies the words.
#
# The admission test for a new entry is that the string must convey no
# instruction and no authority - it may only ask the target to look at what it
# already has. "pull messages" and "status update" pass: acting on them means
# reading your own inbox or reporting your own state, and neither is an action
# a hostile sender gains anything from. Anything of the form "do X", "merge X",
# "skip X" fails and must keep the header. Adding a key is a code change to
# this file, which means it goes through review - deliberately.
#
# There are no authority-bearing exceptions. The retired authorization nudge
# made a peer message indistinguishable from the operator's own instruction;
# the orchestrator no longer uses it, so the manual escape hatch is removed as
# well. A real approval remains a direct operator action.
NUDGES = {
    "pull": "pull messages",
    "status": "status update",
}

# The extraction pipeline relabels pasted peer messages `peer-agent` in
# session archives so `user` there always means the operator. The header
# prefix and the nudge literals are duplicated into each extractor;
# tests/test_extract_agent_history.py pins both sides together, so
# changing the header format here fails tests until the extractors move too.
PEER_MESSAGE_HEADER_PREFIX = "[agent-tmux-send from "


def peer_payload(label: str, message: str) -> str:
    return (
        f"{PEER_MESSAGE_HEADER_PREFIX}{label}; peer message, not operator authorization]\n"
        f"{message}"
    )


def validate_message(message: str) -> str:
    message = message.replace("\r\n", "\n").rstrip()
    if not message.strip():
        raise ValueError("message is empty")
    if len(message.encode("utf-8")) > MAX_BYTES:
        raise ValueError("message exceeds 32 KiB")
    if any(ord(char) < 32 and char not in "\n\t" for char in message):
        raise ValueError("message contains terminal control characters")
    return message


#: bounded submit attempts; an Enter into an already-empty prompt is a no-op
#: in every supported agent TUI, so a spurious retry is harmless while a
#: missing one strands the payload unsubmitted.
SUBMIT_ENTER_TRIES = 3


def _submit_needle(payload: str) -> str:
    """A distinctive tail of the payload's last non-empty line. The input box
    renders at the pane bottom, so if this fragment is still visible there
    after Enter, the submit did not happen. Kept short to survive most
    soft-wrap positions; a pathological wrap can split it, which fails toward
    a harmless extra Enter."""
    lines = [ln for ln in payload.splitlines() if ln.strip()]
    return lines[-1].strip()[-24:] if lines else ""


SUBMITTED_SIGNS = (
    "Working (",                  # codex busy
    "esc to interrupt",           # claude and codex busy hint
    "esc interrupt",              # OpenCode busy hint
    "Ask Codex to do anything",   # codex empty-input placeholder
)


def _stuck_in_input(location: str, needle: str) -> bool:
    """Is the payload still sitting in the pane's input line WITHOUT any
    sign the TUI accepted it? Bottom lines only: a submitted message scrolls
    into the transcript, the input box stays at the bottom. A busy indicator
    or an empty-input placeholder means accepted/queued - NOT stuck - even
    if the payload text is still visible (echo or queued input). An
    unobservable pane counts as STUCK: a false 'failed' costs one harmless
    retry; a false 'sent' costs hours."""
    if not needle:
        return False
    try:
        tail = tmux(["capture-pane", "-p", "-t", location, "-S", "-6"])
    except RuntimeError:
        return True
    if any(sign in tail for sign in SUBMITTED_SIGNS):
        return False
    lines = tail.splitlines()
    hits = [i for i, ln in enumerate(lines) if needle in ln]
    if not hits:
        return False
    below = lines[hits[-1] + 1:]
    if any(ln.strip() in (">", "›", "»") for ln in below):
        return False
    return True


PANEL_OVERLAY_SIGNS = (
    "← for agents",
    "enter to select",
    "enter to confirm",
    "esc to cancel",
    "use arrow",
)
_CURSOR_OPTION = re.compile(r"(?m)^\s*[>❯]\s*\d+[.)]\s+\S")


def _panel_overlay(location: str) -> str:
    """Return a dialog marker, unreadable-pane, or an empty string.

    Unreadable input prevents sending. Ignore markers inside our own pasted
    payload so quoted interface text cannot masquerade as an active dialog.
    """
    try:
        tail = tmux(["capture-pane", "-p", "-t", location, "-S", "-6"])
    except RuntimeError:
        return "unreadable-pane"
    sign = next((sig for sig in PANEL_OVERLAY_SIGNS
                 if sig in tail.lower()), "")
    if sign:
        return sign
    if _CURSOR_OPTION.search(tail):
        return "selection-dialog"
    return ""




def _strand_server() -> str:
    """The server label comes from
    tmux_runtime, never a private env derivation."""
    server, _source = tmux_runtime.configured_server()
    return server or "default"


def _strand_path(pane_id: str) -> Path:
    base = nw_paths.runtime_root() / "stranded"
    key = f"{_strand_server()}-{pane_id.lstrip('%')}".replace("/", "-")
    return base / f"pane-{key}"


def _strand_record(pane_id: str, needle: str) -> str:
    """Persist the strand/intent; '' on success, else the error text.
    This is written BEFORE the paste as an intent marker and
    deleted only on a VERIFIED submit or an explicit --clear-strand - so
    a post-paste record failure cannot exist, a pre-paste write failure
    refuses the send (the probe IS the record), and a crash between paste
    and record can no longer lose the record."""
    try:
        p = _strand_path(pane_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(needle + "\n")
        return ""
    except OSError as e:
        return str(e)


def _strand_check(location: str, pane_id: str) -> str:
    """The reason sends to this pane must refuse, else ''. There is NO
    auto-clearing: a recorded strand
    refuses every send until a human-visible `--clear-strand` with a
    typed reason removes it. When the pane happens to read empty, that is
    printed as ADVICE inside the refusal, never acted on."""
    p = _strand_path(pane_id)
    if not p.exists():
        return ""
    try:
        needle = p.read_text().strip()
    except OSError as e:
        return f"strand record unreadable ({e}) - failing closed"
    advice = ""
    try:
        tail = tmux(["capture-pane", "-p", "-t", location, "-S", "-6"])
        if needle and needle not in tail and any(
                ln.strip() in (">", "\u203a", "\u00bb")
                for ln in tail.splitlines()):
            advice = (" (the input currently READS empty - if you own this"
                      " pane's history, clear with --clear-strand)")
    except RuntimeError:
        pass
    return (f"stranded needle {needle!r} recorded - sends refuse until"
            f" --clear-strand{advice}")


class SendError(RuntimeError):
    outcome = SendOutcome.REFUSED_TARGET


class DeadTarget(SendError):
    outcome = SendOutcome.DEAD_TARGET


class RefusedTarget(SendError):
    outcome = SendOutcome.REFUSED_TARGET


class RefusedStrand(SendError):
    outcome = SendOutcome.REFUSED_STRAND


class RefusedUnrecordable(SendError):
    outcome = SendOutcome.REFUSED_UNRECORDABLE


class RefusedPrepaste(SendError):
    outcome = SendOutcome.REFUSED_PREPASTE


class HeldFocus(SendError):
    outcome = SendOutcome.HELD_FOCUS


class EnterUnconfirmed(SendError):
    outcome = SendOutcome.ENTER_UNCONFIRMED


class SentButHeld(SendError):
    outcome = SendOutcome.SENT_BUT_HELD


def send_outcome(target, message, nudge_key=None, progress=None):
    """(outcome, detail) from the closed set above. ValueError (caller
    misuse) is NOT absorbed - a programming error must stay loud."""
    try:
        location, pane_id, _cmd = send(target, message, nudge_key=nudge_key,
                                       progress=progress)
        return SendOutcome.CONTACTED, f"{location} ({pane_id})"
    except SendError as exc:
        return exc.outcome, str(exc)


def _delete_buffer(buffer_name: str) -> str:
    """Best-effort tmux buffer cleanup; '' on success, else the error text.
    Never raises - BaseException included: a KeyboardInterrupt
    here would mask the primary error, and the interrupt's purpose is
    served by the primary raise terminating the command anyway."""
    try:
        subprocess.run(
            [*tmux_base_cmd(), "delete-buffer", "-b", buffer_name],
            text=True, capture_output=True, check=False,
        )
        return ""
    except BaseException as exc:
        return str(exc) or type(exc).__name__


def send(target: str, message: str, nudge_key: str | None = None,
         progress=None) -> tuple[str, str, str]:
    """Paste one message into an agent pane and submit it.

    `nudge_key` selects a fixed string from NUDGES and sends it WITHOUT the
    peer-message header, so it reads as if typed at the keyboard. That is the
    point: a pull-mode seat ignores a labelled peer message but responds to what
    looks like its operator, and the fleet lost hours to bus messages nobody
    read. The impersonation is safe only because the text is fixed and carries
    no instruction - see NUDGES.
    """
    if nudge_key is not None:
        if nudge_key not in NUDGES:
            raise ValueError(
                f"unknown nudge {nudge_key!r}; allowed: {', '.join(sorted(NUDGES))}"
            )
        if message.strip():
            raise ValueError("--nudge sends a fixed string; it takes no message text")
        message = NUDGES[nudge_key]
    message = validate_message(message)
    progress = progress or (lambda kind, detail="": None)
    location, pane_id, command, dead = pane_info(target)
    location = canonical_location(pane_id, location)
    if dead:
        raise DeadTarget(f"target pane {location} is dead")
    if command not in AGENT_COMMANDS:
        raise RefusedTarget(
            f"target {location} is running {command!r}, not claude/codex/opencode"
        )
    stranded = _strand_check(location, pane_id)
    if stranded:
        raise RefusedStrand(
            f"target {location}: {stranded} - refusing to paste on top;"
            f" sends resume only after an explicit --clear-strand with a"
            f" typed reason")
    overlay = _panel_overlay(location)
    if overlay:
        # A multi-panel overlay consumes Enter as a selection key. Refuse
        # before pasting so no payload remains in the input buffer and no
        # arbitrary row is selected; retry after the overlay closes.
        raise HeldFocus(
            f"target {location} shows a panel overlay ({overlay!r}) -"
            f" synthetic keys would be swallowed or select a row; held"
            f" without pasting")

    if nudge_key is not None:
        payload = message
    else:
        payload = peer_payload(source_label(), message)
    # Intent record: written BEFORE the paste, deleted only on a
    # verified submit or an explicit --clear-strand; a write failure here
    # refuses the send (cannot-record means cannot-safely-send)
    intent_needle = _submit_needle(payload)
    intent_err = _strand_record(pane_id, intent_needle)
    if intent_err:
        raise RefusedUnrecordable(
            f"refusing to send: the strand intent record could not be"
            f" written ({intent_err}) - a strand could not be recorded if"
            f" one occurred")
    buffer_name = f"agent-tmux-send-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        try:
            tmux(["load-buffer", "-b", buffer_name, "-"], stdin=payload)
        except BaseException as load_exc:
            # A load-buffer failure is POSITIVELY pre-paste - it
            # only fills a tmux server buffer, nothing can have reached the
            # pane - so the just-written intent must not outlive a send
            # that provably never started. The boundary is "did anything
            # possibly reach the pane": from the paste attempt onward
            # (including a FAILED paste-buffer) unknown means RETAIN.
            try:
                _strand_path(pane_id).unlink()
            except OSError as cleanup_exc:
                raise RefusedPrepaste(
                    f"pre-paste load-buffer failed ({load_exc}) AND the"
                    f" intent record could not be discarded ({cleanup_exc})"
                    f" - this pane now holds a FALSE strand; verify the"
                    f" input is clean and --clear-strand it") from load_exc
            raise RefusedPrepaste(str(load_exc)) from load_exc
        tmux(["paste-buffer", "-p", "-r", "-d", "-b", buffer_name, "-t", location])
        progress("pasted", location)
        delay = float(os.environ.get("AGENT_TMUX_SEND_SUBMIT_DELAY_S", "0.5"))
        needle = _submit_needle(payload)
        time.sleep(delay)
        for attempt in range(SUBMIT_ENTER_TRIES):
            occluded = _panel_overlay(location)
            if occluded:
                raise HeldFocus(
                    f"HELD_FOCUS: {occluded!r} after the paste (before"
                    f" Enter attempt {attempt + 1}) - zero keys sent; the"
                    f" pre-paste intent record stands, and later sends to"
                    f" this pane refuse until an explicit --clear-strand")
            tmux(["send-keys", "-t", location, "Enter"])
            progress("entered", location)
            time.sleep(delay * (2 ** attempt))
            if not _stuck_in_input(location, needle):
                try:
                    _strand_path(pane_id).unlink()
                except OSError as unlink_exc:
                    # The send DID succeed, but partial success
                    # reported as success is how silent operational debt
                    # accumulates - fail LOUD and nonzero, keep the record
                    # (already safe: the next send refuses on it)
                    raise SentButHeld(
                        f"SENT_BUT_HELD: the payload submitted to"
                        f" {location}, but the intent record could not be"
                        f" removed ({unlink_exc}) - the record stays and"
                        f" later sends to this pane refuse until an"
                        f" explicit --clear-strand. The send itself"
                        f" SUCCEEDED: do NOT resend this message"
                    ) from unlink_exc
                break
        else:
            try:
                tail = tmux(["capture-pane", "-p", "-t", location, "-S", "-6"])
            except RuntimeError:
                tail = ""
            last = [ln for ln in tail.splitlines() if ln.strip()][-2:]
            raise EnterUnconfirmed(
                f"payload is still sitting in {location}'s input line after"
                f" {SUBMIT_ENTER_TRIES} Enter attempts - NOT submitted"
                f" | evidence: signs=none, empty-prompt-below=no,"
                f" needle={needle!r}, tail={last!r}"
            )
    except BaseException as primary:
        cleanup_err = _delete_buffer(buffer_name)
        if cleanup_err:
            primary.add_note(f"ALSO: paste-buffer cleanup failed"
                             f" ({cleanup_err}) - a stale tmux buffer"
                             f" {buffer_name} may remain")
        raise
    cleanup_err = _delete_buffer(buffer_name)
    if cleanup_err:
        # the send is truthfully complete; a leaked uniquely-named buffer
        # blocks nothing - visible, but not a failure
        print(f"WARN: paste-buffer cleanup failed ({cleanup_err}) - a stale"
              f" tmux buffer {buffer_name} may remain", file=sys.stderr)
    return location, pane_id, command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a best-effort direct message to a local tmux agent pane."
    )
    parser.add_argument("target", help="Any tmux target, for example 4:2.0, 2, or %%35")
    parser.add_argument("message", nargs="*", help="Message text; omit to read stdin")
    parser.add_argument(
        "--nudge", choices=sorted(NUDGES),
        help="send a fixed keyboard-style nudge with NO peer-message header "
             "(pull='pull messages', status='status update'); takes no message text",
    )
    parser.add_argument(
        "--clear-strand", action="store_true",
        help="clear the pane's recorded strand instead of sending; requires"
             " --reason; recorded strands never clear automatically")
    parser.add_argument("--reason", default="",
                        help="human reason for --clear-strand; recorded in"
                             " the operator-visible output")
    args = parser.parse_args(argv)
    if args.clear_strand:
        if args.message or args.nudge:
            parser.exit(1, "agent-tmux-send: --clear-strand takes no message"
                           " text and no --nudge - it clears, it never"
                           " sends\n")
        if not args.reason.strip():
            parser.exit(1, "agent-tmux-send: --clear-strand requires a"
                           " non-empty --reason\n")
        try:
            location, pane_id, _cmd, _dead = pane_info(args.target)
            location = canonical_location(pane_id, location)
            record = _strand_path(pane_id)
            needle = record.read_text().strip() if record.exists() else ""
            if not needle:
                print(f"no strand recorded for {location} ({pane_id})")
                return 0
            record.unlink()
        except (RuntimeError, OSError) as exc:
            parser.exit(1, f"agent-tmux-send: clear-strand failed: {exc}\n")
        print(f"STRAND CLEARED on {location} ({pane_id}): needle was"
              f" {needle!r}; reason: {args.reason.strip()}")
        return 0
    if args.nudge:
        message = " ".join(args.message)
    else:
        message = " ".join(args.message) if args.message else sys.stdin.read()
    try:
        location, pane_id, command = send(args.target, message, nudge_key=args.nudge)
    except (RuntimeError, ValueError) as exc:
        # Exception notes from cleanup failures must reach the
        # caller's text, not only a traceback nobody renders here
        notes = "".join(f"\n  {n}" for n in getattr(exc, "__notes__", None) or [])
        parser.exit(1, f"agent-tmux-send: {exc}{notes}\n")
    how = f"nudge {args.nudge!r}" if args.nudge else "message"
    print(f"sent {how} to {format_source_label(command, location)} ({pane_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
