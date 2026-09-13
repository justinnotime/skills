"""Strict decoder for DeepSeek Harness session formats version 0 and version 3.

Version 3 (DeepSeek Harness 0.1.5, file name ``session.v3.jsonl.zstd``) keeps
the version 0 event envelope. It replaces the header's ``seedLength`` with a
boolean ``isSeeded`` (the inherited cut then comes from the ``session/end-seed``
marker), surfaces the system prompt as a ``system/message`` event, embeds the
assistant stream inside ``assistant/message``, and adds the released event
types listed below.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..model import (
    DecodeBatch,
    DecodedEvent,
    DecodedSession,
    Diagnostic,
    FormatObservations,
    RejectedSession,
    SourceSnapshot,
)

SESSION_FORMAT_VERSIONS = {0, 3}
MAX_SAFE_INTEGER = 2**53 - 1
ZSTD_MAGIC = 0xFD2FB528
PACKED_ROW_TYPES = {"text-chunks", "reasoning-chunks", "tool-call-chunks"}
SURFACE_EVENT_TYPES = {
    "user/message",
    "assistant/message",
    "system/message",
    "tool/result",
}
KNOWN_EVENT_TYPES = {
    "agent-preset/selected",
    "agent/inbox/spliced",
    "approval/asked",
    "approval/decided",
    "approval/policy",
    "assistant/attempt",
    "assistant/chunk",
    "assistant/message",
    "command/done",
    "command/run",
    "compaction/end",
    "compaction/prune",
    "compaction/start",
    "compaction/summary",
    "feedback/message-delete",
    "feedback/message-put",
    "feedback/record",
    "goal/change",
    "hook/invoked",
    "hook/result",
    "llm/retry",
    "llm/retry-started",
    "permission/preset",
    "plan/mode",
    "request/context",
    "request/header",
    "sandbox/mode",
    "schedule/change",
    "session-log-deepseek/delivery-accepted",
    "session/end-seed",
    "session/title",
    "session/title-llm-request",
    "step/end",
    "step/start",
    "subagent/descriptor",
    "system/message",
    "team/message/queued",
    "todo/write",
    "tool-workflow/agent-end",
    "tool-workflow/agent-start",
    "tool-workflow/run-end",
    "tool-workflow/run-start",
    "tool/call",
    "tool/code-dispatch",
    "tool/code-dispatch-start",
    "tool/ptc-dispatch",
    "tool/ptc-dispatch-start",
    "tool/result",
    "turn/end",
    "turn/start",
    "user/message",
    "v3/opaque-released-event",
    "web/deepseek-search-llm-request",
}
EVENT_KEYS = {
    "type",
    "seq",
    "time",
    "data",
    "surfaceOp",
    "sourceEventSeqs",
    "ignorable",
}
HEADER_KEYS = {
    "type",
    "version",
    "id",
    "createdAt",
    "cwd",
    "seedLength",
    "isSeeded",
    "delegationDepth",
    "parentSession",
    "origin",
    "agentPreset",
}


class _DshFormatError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ZstdUnavailable(RuntimeError):
    pass


def _safe_nonnegative_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_SAFE_INTEGER
    )


def _safe_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
    )


def _scan_zstd_frames(data: bytes) -> tuple[list[tuple[int, int]], int | None]:
    """Return complete frame bounds and the start of an incomplete tail."""

    frames: list[tuple[int, int]] = []
    offset = 0
    while offset < len(data):
        start = offset
        if len(data) - offset < 4:
            return frames, start
        if int.from_bytes(data[offset : offset + 4], "little") != ZSTD_MAGIC:
            raise _DshFormatError("DSH_ZSTD_FRAME_MAGIC_INVALID")
        offset += 4
        if offset == len(data):
            return frames, start

        descriptor = data[offset]
        offset += 1
        if descriptor & 0x18:
            raise _DshFormatError("DSH_ZSTD_FRAME_HEADER_INVALID")
        content_size_flag = descriptor >> 6
        single_segment = bool(descriptor & 0x20)
        checksum = bool(descriptor & 0x04)
        dictionary_flag = descriptor & 0x03
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (
            (1 if content_size_flag == 0 and single_segment else 0)
            if content_size_flag == 0
            else 1 << content_size_flag
        )
        remaining_header = (
            (0 if single_segment else 1) + dictionary_bytes + content_size_bytes
        )
        if len(data) - offset < remaining_header:
            return frames, start
        offset += remaining_header

        while True:
            if len(data) - offset < 3:
                return frames, start
            block_header = int.from_bytes(data[offset : offset + 3], "little")
            offset += 3
            last_block = bool(block_header & 1)
            block_type = (block_header >> 1) & 0x03
            block_size = block_header >> 3
            if block_type == 0x03:
                raise _DshFormatError("DSH_ZSTD_BLOCK_INVALID")
            payload_bytes = 1 if block_type == 0x01 else block_size
            if len(data) - offset < payload_bytes:
                return frames, start
            offset += payload_bytes
            if last_block:
                break
        if checksum:
            if len(data) - offset < 4:
                return frames, start
            offset += 4
        frames.append((start, offset))
    return frames, None


def _zstd_module() -> Any:
    try:
        from compression import zstd
    except ImportError as error:
        raise _ZstdUnavailable from error
    return zstd


def _decode_zstd(data: bytes, *, allow_torn_current_frame: bool) -> tuple[bytes, bool]:
    zstd = _zstd_module()
    frames, torn_start = _scan_zstd_frames(data)
    if not frames:
        raise _DshFormatError("DSH_ZSTD_HEADER_FRAME_INCOMPLETE")

    decoded: list[bytes] = []
    try:
        for start, end in frames:
            decoded.append(zstd.decompress(data[start:end]))
    except (zstd.ZstdError, EOFError) as error:
        raise _DshFormatError("DSH_ZSTD_COMPLETED_FRAME_INVALID") from error

    if not decoded[0].endswith(b"\n") or decoded[0].count(b"\n") != 1:
        raise _DshFormatError("DSH_ZSTD_HEADER_FRAME_INVALID")

    if torn_start is not None and not allow_torn_current_frame:
        raise _DshFormatError("DSH_TORN_CURRENT_FRAME_NOT_ALLOWED")
    if torn_start is not None:
        try:
            tail = zstd.ZstdDecompressor().decompress(data[torn_start:])
        except (zstd.ZstdError, EOFError):
            tail = b""
        decoded.append(tail[: tail.rfind(b"\n") + 1])
    elif decoded and not decoded[-1].endswith(b"\n"):
        raise _DshFormatError("DSH_ZSTD_COMPLETED_FRAME_JSONL_INCOMPLETE")
    return b"".join(decoded), torn_start is not None


def _json_records(
    data: bytes, *, allow_torn_tail: bool
) -> tuple[list[dict[str, Any]], bool]:
    incomplete = False
    if not data.endswith(b"\n"):
        if not allow_torn_tail:
            raise _DshFormatError("DSH_JSONL_FINAL_NEWLINE_MISSING")
        newline = data.rfind(b"\n")
        if newline < 0:
            raise _DshFormatError("DSH_JSONL_HEADER_INCOMPLETE")
        data = data[: newline + 1]
        incomplete = True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _DshFormatError("DSH_JSONL_UTF8_INVALID") from error

    records: list[dict[str, Any]] = []
    for line in text[:-1].split("\n"):
        try:
            record = json.loads(
                line,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise _DshFormatError("DSH_JSONL_RECORD_INVALID") from error
        if not isinstance(record, dict):
            raise _DshFormatError("DSH_JSONL_RECORD_NOT_OBJECT")
        records.append(record)
    if not records:
        raise _DshFormatError("DSH_SESSION_EMPTY")
    return records, incomplete


def _valid_header(header: Mapping[str, Any]) -> bool:
    return (
        set(header).issubset(HEADER_KEYS)
        and header.get("type") == "session"
        and not isinstance(header.get("version"), bool)
        and header.get("version") in SESSION_FORMAT_VERSIONS
        and isinstance(header.get("id"), str)
        and bool(header["id"])
        and _safe_nonnegative_integer(header.get("createdAt"))
        and _safe_nonnegative_integer(header.get("delegationDepth"))
        and (
            "cwd" not in header
            or (
                isinstance(header["cwd"], str)
                and bool(header["cwd"])
                and Path(header["cwd"]).is_absolute()
            )
        )
        and ("parentSession" not in header or isinstance(header["parentSession"], str))
        and (
            "seedLength" not in header
            or _safe_nonnegative_integer(header["seedLength"])
        )
        and (
            "isSeeded" not in header
            or (header["version"] != 0 and isinstance(header["isSeeded"], bool))
        )
        and ("origin" not in header or header["origin"] == "subagent")
        and ("agentPreset" not in header or isinstance(header["agentPreset"], str))
    )


def _valid_provider_model(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("provider"), str)
        and bool(value["provider"])
        and isinstance(value.get("model"), str)
        and bool(value["model"])
    )


def _valid_message(message: object, role: str) -> bool:
    if (
        not isinstance(message, dict)
        or not isinstance(message.get("id"), str)
        or not message["id"]
        or message.get("role") != role
        or not isinstance(message.get("content"), list)
    ):
        return False
    source = message.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("kind"), str)
        or not source["kind"]
    ):
        return False
    return role != "assistant" or (
        source["kind"] == "model" and _valid_provider_model(source)
    )


def _valid_event(record: Mapping[str, Any], expected_seq: int) -> bool:
    if not set(record).issubset(EVENT_KEYS):
        return False
    record_type = record.get("type")
    if (
        not isinstance(record_type, str)
        or record_type == "request/header-delta"
        or record.get("seq") != expected_seq
        or not _safe_nonnegative_integer(record.get("seq"))
        or not _safe_integer(record.get("time"))
        or "data" not in record
        or ("ignorable" in record and record["ignorable"] is not True)
    ):
        return False
    data = record["data"]
    if record_type == "user/message":
        return _valid_message(data, "user")
    if record_type == "assistant/message":
        return isinstance(data, dict) and _valid_message(
            data.get("message"), "assistant"
        )
    if record_type == "tool/result":
        if not isinstance(data, dict) or not _valid_message(
            data.get("message"), "user"
        ):
            return False
        message = data["message"]
        source = message["source"]
        content = message["content"]
        return (
            source.get("kind") == "tool"
            and isinstance(source.get("callId"), str)
            and bool(source["callId"])
            and len(content) == 1
            and isinstance(content[0], dict)
            and content[0].get("type") == "tool-result"
            and isinstance(content[0].get("content"), list)
            and content[0].get("toolCallId") == source["callId"]
        )
    if record_type == "request/header":
        if not isinstance(data, dict) or not isinstance(data.get("header"), dict):
            return False
        header = data["header"]
        config = header.get("config")
        if not _valid_provider_model(config) or data.get("reason") == "fallback":
            return False
        reasoning = config.get("reasoningEffort")
        if reasoning is not None and (not isinstance(reasoning, str) or not reasoning):
            return False
        defaults = header.get("adapterDefaults")
        if defaults is not None and (
            not isinstance(defaults, dict)
            or not set(defaults).issubset({"reasoningEffort", "maxTokens"})
            or any(marker is not True for marker in defaults.values())
            or any(key not in config for key in defaults)
        ):
            return False
    return True


def _packed_row_length(record: Mapping[str, Any], expected_seq: int) -> int:
    record_type = record.get("type")
    if set(record) != {"type", "seq0", "time0", "data"}:
        return 0
    if record.get("seq0") != expected_seq or not _safe_integer(record.get("time0")):
        return 0
    data = record.get("data")
    if not isinstance(data, dict):
        return 0
    payload_key = "args" if record_type == "tool-call-chunks" else "texts"
    expected_keys = {"turn", "step", "index", "dt", payload_key}
    if record_type == "tool-call-chunks":
        expected_keys.add("id")
        if "name" in data:
            expected_keys.add("name")
    payload = data.get(payload_key)
    gaps = data.get("dt")
    if (
        set(data) != expected_keys
        or not isinstance(data.get("turn"), (int, float))
        or isinstance(data.get("turn"), bool)
        or not isinstance(data.get("step"), (int, float))
        or isinstance(data.get("step"), bool)
        or not isinstance(data.get("index"), (int, float))
        or isinstance(data.get("index"), bool)
        or not isinstance(payload, list)
        or not payload
        or any(not isinstance(item, str) for item in payload)
        or not isinstance(gaps, list)
        or len(gaps) != len(payload) - 1
        or any(not _safe_integer(gap) for gap in gaps)
    ):
        return 0
    if record_type == "tool-call-chunks" and (
        not isinstance(data.get("id"), str)
        or ("name" in data and not isinstance(data["name"], str))
    ):
        return 0
    if expected_seq + len(payload) - 1 > MAX_SAFE_INTEGER:
        return 0
    timestamp = record["time0"]
    for gap in gaps:
        timestamp += gap
        if not _safe_integer(timestamp):
            return 0
    return len(payload)


def _record_start_sequence(record: Mapping[str, Any]) -> object:
    if record.get("type") in PACKED_ROW_TYPES:
        return record.get("seq0")
    return record.get("seq")


def _tool_result_rewrite_is_valid(
    record: Mapping[str, Any],
    shadowed: list[int],
    event_records: Mapping[int, Mapping[str, Any]],
) -> bool:
    if len(shadowed) != 1:
        return False
    original = event_records.get(shadowed[0])
    if not isinstance(original, dict) or original.get("type") != "tool/result":
        return False
    original_data = copy.deepcopy(original.get("data"))
    replacement_data = copy.deepcopy(record.get("data"))
    try:
        original_data["message"]["content"][0]["content"] = None
        replacement_data["message"]["content"][0]["content"] = None
    except (KeyError, IndexError, TypeError):
        return False
    return original_data == replacement_data


def _apply_surface_metadata(
    record: Mapping[str, Any],
    record_type: object,
    seq: int,
    surface_nodes: list[int],
    event_records: Mapping[int, Mapping[str, Any]],
) -> bool:
    has_surface = "surfaceOp" in record
    has_sources = "sourceEventSeqs" in record
    if record_type not in SURFACE_EVENT_TYPES:
        return not has_surface and not has_sources
    if not has_surface:
        return False
    surface_op = record["surfaceOp"]
    replacement: Mapping[str, Any] | None = None
    if surface_op != "append":
        if (
            not isinstance(surface_op, dict)
            or set(surface_op) != {"op", "start", "end"}
            or surface_op.get("op") != "replace"
            or not _safe_nonnegative_integer(surface_op.get("start"))
            or not _safe_nonnegative_integer(surface_op.get("end"))
        ):
            return False
        replacement = surface_op
    sources: list[int] | None = None
    if has_sources:
        sources = record["sourceEventSeqs"]
        if (
            not isinstance(sources, list)
            or any(
                not _safe_nonnegative_integer(source) or source >= seq
                for source in sources
            )
            or len(sources) != len(set(sources))
            or (not sources and record_type != "assistant/message")
        ):
            return False
    if replacement is None:
        surface_nodes.append(seq)
        return True
    try:
        start = surface_nodes.index(replacement["start"])
        end = surface_nodes.index(replacement["end"])
    except ValueError:
        return False
    if start > end:
        return False
    shadowed = surface_nodes[start : end + 1]
    if sources is None or any(item not in sources for item in shadowed):
        return False
    if record_type == "tool/result" and not _tool_result_rewrite_is_valid(
        record, shadowed, event_records
    ):
        return False
    surface_nodes[start : end + 1] = [seq]
    return True


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ).strip()


def _source_kind(message: Mapping[str, Any]) -> object:
    source = message.get("source")
    return source.get("kind") if isinstance(source, dict) else None


def _timestamp(milliseconds: int) -> datetime | None:
    try:
        return datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _observations(
    recognized: Counter[str], unknown: Counter[str], user_markers: int, accepted: int
) -> FormatObservations:
    return FormatObservations(
        recognized_record_counts=dict(sorted(recognized.items())),
        unknown_record_counts=dict(sorted(unknown.items())),
        recognizable_user_markers=user_markers,
        accepted_direct_user_events=accepted,
    )


class DshDecoder:
    harness = "dsh"

    def capabilities(self) -> tuple[str, ...]:
        result = ["plain-jsonl"]
        try:
            _zstd_module()
        except _ZstdUnavailable:
            pass
        else:
            result.append("zstd")
        return tuple(result)

    def decode(self, snapshot: SourceSnapshot, *, observer=None) -> DecodeBatch:
        if snapshot.payload is None:
            return self._failure(snapshot, "DSH_SNAPSHOT_PAYLOAD_MISSING", "invalid")

        compression = snapshot.decoder_options.get("compression", "auto")
        if compression not in {"auto", "plain", "zstd"}:
            return self._failure(snapshot, "DSH_COMPRESSION_OPTION_INVALID", "invalid")
        allow_torn = snapshot.decoder_options.get("allow_torn_current_frame", False)
        if not isinstance(allow_torn, bool):
            return self._failure(snapshot, "DSH_TORN_FRAME_POLICY_INVALID", "invalid")
        compressed = compression == "zstd" or (
            compression == "auto"
            and (
                snapshot.payload.startswith(ZSTD_MAGIC.to_bytes(4, "little"))
                or snapshot.path.name.endswith(".zstd")
            )
        )
        try:
            if compressed:
                decoded, torn = _decode_zstd(
                    snapshot.payload, allow_torn_current_frame=allow_torn
                )
                records, json_torn = _json_records(decoded, allow_torn_tail=False)
                torn = torn or json_torn
            else:
                records, torn = _json_records(
                    snapshot.payload, allow_torn_tail=allow_torn
                )
        except _ZstdUnavailable:
            return self._failure(snapshot, "DSH_ZSTD_CAPABILITY_MISSING", "incomplete")
        except _DshFormatError as error:
            return self._failure(snapshot, error.code, "invalid")

        if observer is not None:
            observer.jsonl(list(enumerate(records)), malformed=int(torn))
            return DecodeBatch(sessions=(), completeness="incomplete" if torn else "complete")
        recognized: Counter[str] = Counter()
        unknown: Counter[str] = Counter()
        user_markers = 0
        accepted = 0
        header = records[0]
        if not _valid_header(header):
            return self._failure(snapshot, "DSH_HEADER_INVALID", "invalid")
        recognized["session"] += 1

        session_id = header["id"]
        if header.get("origin") == "subagent" or header["delegationDepth"] > 0:
            return DecodeBatch(
                sessions=(),
                observations=_observations(recognized, unknown, 0, 0),
                rejected_sessions=(RejectedSession(session_id, "DSH_SUBAGENT"),),
                completeness="incomplete" if torn else "complete",
                diagnostics=(
                    (
                        Diagnostic(
                            "DSH_TORN_TAIL_RECOVERED", snapshot.source_id, session_id
                        ),
                    )
                    if torn
                    else ()
                ),
            )

        seed_length = header.get("seedLength", 0)
        expected_seq = 0
        surface_nodes: list[int] = []
        event_records: dict[int, Mapping[str, Any]] = {}
        events: list[DecodedEvent] = []
        seen_messages: set[str] = set()
        pending_seed_rollback: tuple[int, int, bool, DecodedEvent | None] | None = None

        for record_index, record in enumerate(records[1:], start=1):
            record_type = record.get("type")
            record_start = _record_start_sequence(record)
            if record_start != expected_seq and pending_seed_rollback is not None:
                (
                    restart_seq,
                    boundary_user_seq,
                    boundary_is_direct,
                    boundary_event,
                ) = pending_seed_rollback
                if record_type != "assistant/chunk" or record_start != restart_seq:
                    return self._failure(snapshot, "DSH_EVENT_INVALID", "invalid")
                expected_seq = restart_seq
                surface_nodes = [
                    sequence
                    for sequence in surface_nodes
                    if sequence <= boundary_user_seq
                ]
                event_records = {
                    sequence: prior
                    for sequence, prior in event_records.items()
                    if sequence <= boundary_user_seq
                }
                events = [boundary_event] if boundary_event is not None else []
                seen_messages = (
                    {boundary_event.message_key}
                    if boundary_event is not None
                    else set()
                )
                user_markers = 1 if boundary_is_direct else 0
                accepted = 1 if boundary_event is not None else 0
            pending_seed_rollback = None
            if isinstance(record_type, str) and record_type in PACKED_ROW_TYPES:
                count = _packed_row_length(record, expected_seq)
                if not count:
                    return self._failure(snapshot, "DSH_PACKED_ROW_INVALID", "invalid")
                recognized[record_type] += 1
                expected_seq += count
                continue
            if not _valid_event(record, expected_seq):
                return self._failure(snapshot, "DSH_EVENT_INVALID", "invalid")
            if not _apply_surface_metadata(
                record, record_type, expected_seq, surface_nodes, event_records
            ):
                return self._failure(
                    snapshot, "DSH_SURFACE_OPERATION_INVALID", "invalid"
                )
            if record_type not in KNOWN_EVENT_TYPES:
                unknown[str(record_type)] += 1
                if record.get("ignorable") is not True:
                    return self._failure(
                        snapshot, "DSH_UNKNOWN_REQUIRED_EVENT", "invalid"
                    )
                expected_seq += 1
                continue

            recognized[str(record_type)] += 1
            event_records[expected_seq] = record
            expected_seq += 1
            seq = record["seq"]
            if record_type == "session/end-seed":
                if (
                    set(record) != {"type", "seq", "time", "data"}
                    or record["data"] != {}
                ):
                    return self._failure(snapshot, "DSH_EVENT_INVALID", "invalid")
                prior = records[record_index - 3 : record_index]
                if (
                    "seedLength" not in header
                    and len(prior) == 3
                    and tuple(item.get("type") for item in prior)
                    == ("user/message", "step/end", "turn/end")
                ):
                    boundary_user_seq = prior[0].get("seq")
                    if (
                        _safe_nonnegative_integer(boundary_user_seq)
                        and prior[1].get("seq") == boundary_user_seq + 1
                        and prior[2].get("seq") == boundary_user_seq + 2
                        and seq == boundary_user_seq + 3
                        and isinstance(prior[0].get("data"), dict)
                    ):
                        boundary_is_direct = (
                            _source_kind(prior[0]["data"]) == "user"
                        )
                        boundary_event = next(
                            (
                                event
                                for event in reversed(events)
                                if event.source_sequence == boundary_user_seq
                                and event.role_hint == "user-like"
                            ),
                            None,
                        )
                        pending_seed_rollback = (
                            boundary_user_seq + 1,
                            boundary_user_seq,
                            boundary_is_direct,
                            boundary_event,
                        )
                # A marker is authoritative over an absent legacy
                # ``seedLength``. Preserve only the boundary user if the next
                # record proves the one supported rollback shape; otherwise
                # every event before the marker is inherited seed history.
                if "seedLength" not in header:
                    events = []
                    seen_messages = set()
                    user_markers = 0
                    accepted = 0
                continue
            if seq < seed_length or record.get("surfaceOp") != "append":
                continue

            data = record["data"]
            message: Mapping[str, Any] | None = None
            role_hint = "user-like"
            if (
                record_type == "user/message"
                and isinstance(data, dict)
                and _source_kind(data) == "user"
            ):
                user_markers += 1
            if (
                record_type == "user/message"
                and isinstance(data, dict)
                and data.get("role") == "user"
                and _source_kind(data) == "user"
            ):
                message = data
            elif record_type == "assistant/message" and isinstance(data, dict):
                candidate = data.get("message")
                if (
                    isinstance(candidate, dict)
                    and candidate.get("role") == "assistant"
                    and _source_kind(candidate) == "model"
                ):
                    message = candidate
                    role_hint = "assistant"
            if message is None:
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or message_id in seen_messages:
                continue
            text = _message_text(message)
            if not text:
                continue
            seen_messages.add(message_id)
            if role_hint == "user-like":
                accepted += 1
            timestamp = _timestamp(record["time"])
            events.append(
                DecodedEvent(
                    source_sequence=seq,
                    timestamp=timestamp,
                    timestamp_quality="exact" if timestamp is not None else "unknown",
                    role_hint=role_hint,
                    text=text,
                    raw_kind=str(record_type),
                    message_key=message_id,
                )
            )

        observations = _observations(recognized, unknown, user_markers, accepted)
        if not any(event.role_hint == "user-like" for event in events):
            diagnostics = (
                (Diagnostic("DSH_TORN_TAIL_RECOVERED", snapshot.source_id, session_id),)
                if torn
                else ()
            )
            return DecodeBatch(
                sessions=(),
                observations=observations,
                rejected_sessions=(RejectedSession(session_id, "DSH_NO_DIRECT_USER"),),
                completeness="incomplete" if torn else "complete",
                diagnostics=diagnostics,
            )

        cwd = header.get("cwd")
        diagnostics = (
            (Diagnostic("DSH_TORN_TAIL_RECOVERED", snapshot.source_id, session_id),)
            if torn
            else ()
        )
        return DecodeBatch(
            sessions=(
                DecodedSession(
                    session_id=session_id,
                    cwd=cwd if isinstance(cwd, str) else None,
                    project_hint=Path(cwd).name if isinstance(cwd, str) else None,
                    conversation_kind="main",
                    events=tuple(events),
                    metadata={"format_version": header["version"]},
                ),
            ),
            observations=observations,
            # A torn current frame is an expected append-in-progress state.
            # Once a direct conversation was recovered, the complete records
            # form a usable session; the diagnostic remains visible. Header-
            # only and rejected sessions above stay incomplete so they cannot
            # authorize cleanup.
            completeness="complete",
            diagnostics=diagnostics,
        )

    @staticmethod
    def _failure(
        snapshot: SourceSnapshot,
        code: str,
        completeness: str,
    ) -> DecodeBatch:
        return DecodeBatch(
            sessions=(),
            completeness=completeness,  # type: ignore[arg-type]
            diagnostics=(Diagnostic(code, snapshot.source_id),),
        )


DECODER = DshDecoder()

__all__ = ["DECODER", "DshDecoder"]
