"""Synthetic contracts from DSH 0.1.5-rc.2 packages, never local sessions."""

from __future__ import annotations

from dataclasses import replace

import pytest
from session_test_support import manifest_data, write_manifest
from test_session_dsh import diagnostic_codes, encode_jsonl, rows_v3, snapshot, zstd
from test_session_dsh_seed_boundary import event, message

from agent_skills.sessions.api import decode_source_snapshots
from agent_skills.sessions.harnesses.dsh import DshDecoder
from agent_skills.sessions.manifest import load_manifest
from agent_skills.sessions.render import render_history, render_prompts


# Exact SessionEventMap payloads from api-session-controller/types,
# tool-present/types, and subagent/catalog in upstream 0.1.5-rc.2.
BOOKKEEPING = [
    (
        "model/selection",
        {"provider": "fixture-provider", "model": "fixture-selected-model"},
    ),
    (
        "model/selection",
        {
            "provider": "fixture-provider",
            "model": "fixture-selected-model",
            "reasoningEffort": "fixture-effort",
        },
    ),
    (
        "deliverables/presented",
        {
            "turn": 1,
            "callId": "fixture-present-call",
            "files": [
                {"path": "fixture-output.txt", "description": "not dialogue"},
                {"path": "fixture-other.txt"},
            ],
        },
    ),
    (
        "subagent/catalog",
        {
            "version": 0,
            "childId": "fixture-child",
            "childCreatedAt": 1_700_000_000_000,
            "mode": "continuable",
            "label": "fixture-child-label",
        },
    ),
    (
        "subagent/catalog",
        {
            "version": 0,
            "childId": "fixture-child",
            "childCreatedAt": 1_700_000_000_000,
            "mode": "one-shot",
        },
    ),
]


def append_event(records, kind, data, **extra):
    records.append(
        {
            "type": kind,
            "seq": records[-1]["seq"] + 1,
            "time": records[-1]["time"] + 1,
            "data": data,
            **extra,
        }
    )
    return records


@pytest.mark.parametrize("kind,data", BOOKKEEPING)
@pytest.mark.parametrize("compressed", [False, True])
def test_required_bookkeeping_preserves_exact_conversation(kind, data, compressed):
    baseline = DshDecoder().decode(snapshot(encode_jsonl(rows_v3())))
    records = append_event(rows_v3(), kind, data)
    if compressed:
        if zstd is None:
            pytest.skip("compression.zstd unavailable")
        payload = zstd.compress(encode_jsonl(records[:1])) + zstd.compress(
            encode_jsonl(records[1:])
        )
    else:
        payload = encode_jsonl(records)

    batch = DshDecoder().decode(snapshot(payload, compressed=compressed))

    assert batch.completeness == "complete"
    assert batch.sessions == baseline.sessions
    assert len(batch.sessions[0].events) == 2
    assert diagnostic_codes(batch) == set()
    assert batch.observations.recognized_record_counts[kind] == 1
    assert batch.observations.unknown_record_counts == {}
    assert batch.observations.accepted_direct_user_events == 1


@pytest.mark.parametrize("kind", sorted({kind for kind, _ in BOOKKEEPING}))
@pytest.mark.parametrize("extra", [{"surfaceOp": "append"}, {"sourceEventSeqs": [5]}])
def test_bookkeeping_cannot_acquire_message_surface_metadata(kind, extra):
    batch = DshDecoder().decode(
        snapshot(encode_jsonl(append_event(rows_v3(), kind, {}, **extra)))
    )
    assert batch.completeness == "invalid"
    assert batch.sessions == ()
    assert diagnostic_codes(batch) == {"DSH_SURFACE_OPERATION_INVALID"}


@pytest.mark.parametrize("kind", ["future/required", "model/selection-new"])
def test_unknown_required_still_invalidates_after_known_bookkeeping(kind):
    records = rows_v3()
    for known, data in BOOKKEEPING:
        append_event(records, known, data)
    append_event(records, kind, {})

    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))

    assert batch.completeness == "invalid"
    assert batch.sessions == ()
    assert diagnostic_codes(batch) == {"DSH_UNKNOWN_REQUIRED_EVENT"}


def test_bookkeeping_does_not_create_direct_user_events():
    records = rows_v3()[:1]
    for seq, (kind, data) in enumerate(BOOKKEEPING):
        records.append({"type": kind, "seq": seq, "time": 0, "data": data})
    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))
    assert batch.completeness == "complete"
    assert batch.sessions == ()
    assert batch.rejected_sessions[0].reason_code == "DSH_NO_DIRECT_USER"
    assert batch.observations.accepted_direct_user_events == 0


def test_bookkeeping_changes_neither_normalized_history_nor_prompts(tmp_path):
    data = manifest_data(
        tmp_path / "source",
        tmp_path / "output",
        harness="dsh",
        source_id="fixture-source",
        node="fixture-origin",
    )
    manifest = load_manifest(
        write_manifest(tmp_path / "manifest.json", data), environ={}
    )
    source = manifest.sources[0]
    baseline_snapshot = snapshot(encode_jsonl(rows_v3()))
    baseline_snapshot = replace(
        baseline_snapshot, source_ref="fixture-source/session.jsonl"
    )
    baseline = decode_source_snapshots(manifest, source, (baseline_snapshot,))
    records = rows_v3()
    for kind, payload in BOOKKEEPING:
        append_event(records, kind, payload)
    changed = replace(baseline_snapshot, payload=encode_jsonl(records))
    result = decode_source_snapshots(manifest, source, (changed,))

    assert len(result.sessions) == len(baseline.sessions) == 1
    actual, expected = result.sessions[0], baseline.sessions[0]
    assert [(event.role, event.text) for event in actual.events] == [
        ("user", "direct fixture"),
        ("assistant", "model fixture"),
    ]
    assert render_history(actual, manifest.output) == render_history(
        expected, manifest.output
    )
    assert render_prompts(actual, manifest.output) == render_prompts(
        expected, manifest.output
    )


def v3_fork_records():
    # Last tagged marker, not last lifecycle/resume marker, owns the fork cut.
    return [
        {**rows_v3()[0], "isSeeded": True},
        event(
            "user/message",
            0,
            message("ancestor", "user", "ancestor fixture", "user"),
            surfaceOp="append",
        ),
        event("session/end-seed", 1, {"inherited": True}),
        event(
            "user/message",
            2,
            message("parent", "user", "parent fixture", "user"),
            surfaceOp="append",
        ),
        event("session/end-seed", 3, {}),
        event("session/end-seed", 4, {"inherited": True}),
        event(
            "user/message",
            5,
            message("own", "user", "own fixture", "user"),
            surfaceOp="append",
        ),
        event("session/end-seed", 6, {}),
        event(
            "assistant/message",
            7,
            {"message": message("answer", "assistant", "answer fixture", "model")},
            surfaceOp="append",
        ),
    ]


@pytest.mark.parametrize("compressed", [False, True])
def test_v3_last_tagged_seed_cut_preserves_own_history_across_resumes(compressed):
    records = v3_fork_records()
    if compressed:
        if zstd is None:
            pytest.skip("compression.zstd unavailable")
        payload = zstd.compress(encode_jsonl(records[:1])) + zstd.compress(
            encode_jsonl(records[1:])
        )
    else:
        payload = encode_jsonl(records)
    batch = DshDecoder().decode(snapshot(payload, compressed=compressed))
    assert batch.completeness == "complete"
    assert diagnostic_codes(batch) == set()
    assert [
        (event.source_sequence, event.role_hint, event.text)
        for event in batch.sessions[0].events
    ] == [
        (5, "user-like", "own fixture"),
        (7, "assistant", "answer fixture"),
    ]
    assert batch.observations.recognizable_user_markers == 1
    assert batch.observations.accepted_direct_user_events == 1


def test_v3_unseeded_resume_marker_does_not_remove_conversation():
    records = rows_v3()
    baseline = DshDecoder().decode(snapshot(encode_jsonl(records)))
    append_event(records, "session/end-seed", {})
    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))
    assert batch.completeness == "complete"
    assert batch.sessions == baseline.sessions
    assert batch.observations.accepted_direct_user_events == 1


@pytest.mark.parametrize(
    "data",
    [
        {"inherited": False},
        {"inherited": 1},
        {"inherited": "true"},
        {"inherited": None},
        {"inherited": True, "extra": True},
        [],
    ],
)
def test_v3_seed_marker_rejects_non_literal_true_or_unknown_payload_fields(data):
    records = v3_fork_records()
    records[5]["data"] = data
    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))
    assert batch.completeness == "invalid"
    assert batch.sessions == ()
    assert diagnostic_codes(batch) == {"DSH_EVENT_INVALID"}


@pytest.mark.parametrize("seeded", [False, True])
def test_v3_seeded_header_must_agree_with_tagged_marker(seeded):
    records = rows_v3()
    records[0]["isSeeded"] = seeded
    if not seeded:
        append_event(records, "session/end-seed", {"inherited": True})
    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))
    assert batch.completeness == "invalid"
    assert batch.sessions == ()
    assert diagnostic_codes(batch) == {"DSH_EVENT_INVALID"}


def test_v3_does_not_use_legacy_seed_length_or_rollback():
    records = v3_fork_records()
    records[0]["seedLength"] = 1
    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))
    assert batch.completeness == "invalid"
    assert diagnostic_codes(batch) == {"DSH_HEADER_INVALID"}

    records = v3_fork_records()
    records[-1]["seq"] = 6
    batch = DshDecoder().decode(snapshot(encode_jsonl(records)))
    assert batch.completeness == "invalid"
    assert diagnostic_codes(batch) == {"DSH_EVENT_INVALID"}
