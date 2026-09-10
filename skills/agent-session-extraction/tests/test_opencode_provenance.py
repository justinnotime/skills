from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from session_test_support import manifest_data, write_manifest

from agent_skills.sessions.api import decode_source_snapshots
from agent_skills.sessions.audit import scan_inventory
from agent_skills.sessions.harnesses.opencode import OpenCodeDecoder
from agent_skills.sessions.manifest import load_manifest
from agent_skills.sessions.pipeline import run_pipeline
from agent_skills.sessions.render import render_history, render_prompts
from agent_skills.sessions.sources import snapshot_candidate, validate_configured_path

MARKER = "[Agent Bus wake]"
WAKE = f"{MARKER}\nSynthetic message available from another agent."
HUMAN = 'Please explain the literal "[Agent Bus wake]" marker.'
START = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)


def write_database(
    path: Path,
    messages: list[tuple[str, str]],
    *,
    session_id: str = "session-example-12345678",
    step_ms: int = 1000,
) -> bytes:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE session (id TEXT, parent_id TEXT, title TEXT, directory TEXT,
                                  time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part (id TEXT, message_id TEXT, time_created INTEGER, data TEXT);
            """
        )
        start_ms = int(START.timestamp() * 1000)
        connection.execute(
            "INSERT INTO session VALUES (?, NULL, ?, ?, ?, ?)",
            (session_id, "Synthetic", "/synthetic/project", start_ms, start_ms),
        )
        # Deliberately insert in reverse order; native roles carry no human/peer flag.
        for index in reversed(range(len(messages))):
            role, text = messages[index]
            stamp = start_ms + index * step_ms
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?)",
                (f"m{index}", session_id, stamp, json.dumps({"role": role})),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?)",
                (
                    f"p{index}",
                    f"m{index}",
                    stamp + 125,
                    json.dumps({"type": "text", "text": text}),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()


def configuration(root: Path, **options) -> dict:
    output = root / "output"
    output.mkdir()
    data = manifest_data(
        root / "opencode.db",
        output,
        harness="opencode",
        discovery_mode="file",
        snapshot="sqlite-readonly",
        publisher="filesystem-atomic",
        **options,
    )
    data["event_policy"]["peer_agent_prefixes"] = []
    data["event_policy"]["peer_agent_exact"] = []
    return data


@pytest.mark.parametrize("configured", [False, True])
def test_plain_sqlite_user_text_uses_only_caller_peer_policy(tmp_path, configured):
    humans = [
        HUMAN,
        '"[Agent Bus wake]"',
        "> [Agent Bus wake]",
        "```text\n[Agent Bus wake]\n```",
        "Discuss this example:\n[Agent Bus wake]\nKeep the quoted text.",
        "[Agent Bus wakeful] is a different marker.",
        "[agent bus wake] has different case.",
        "Agent Bus messages, agents, wake notifications, and tools are our topic.",
    ]
    messages = [
        ("user", WAKE),
        ("user", MARKER),
        ("user", f" \n{WAKE}\n "),
        *(("user", text) for text in humans),
        ("assistant", f"{MARKER} is the marker in this explanation."),
    ]
    database = tmp_path / "opencode.db"
    original = write_database(database, messages)
    data = configuration(tmp_path)
    data["event_policy"]["peer_agent_prefixes"] = [MARKER] if configured else []
    manifest = load_manifest(
        write_manifest(tmp_path / "manifest.json", data), environ={}
    )
    source = manifest.sources[0]
    frozen = snapshot_candidate(source, validate_configured_path(source), database)

    decoded = OpenCodeDecoder().decode(frozen)
    assert decoded.completeness == "complete"
    assert len(decoded.sessions) == 1
    assert [event.role_hint for event in decoded.sessions[0].events] == (
        ["user-like"] * (len(messages) - 1) + ["assistant"]
    )
    normalized = decode_source_snapshots(manifest, source, (frozen,))
    assert len(normalized.sessions) == 1
    session = normalized.sessions[0]
    expected_roles = ["peer-agent" if configured else "user"] * 3
    expected_roles += ["user"] * len(humans) + ["assistant"]
    assert [event.role for event in session.events] == expected_roles
    assert [event.text for event in session.events] == [
        text.strip() for _, text in messages
    ]
    assert [event.sequence for event in session.events] == list(range(len(messages)))
    assert [event.timestamp for event in session.events] == [
        START + timedelta(seconds=index, milliseconds=125)
        for index in range(len(messages))
    ]
    assert all(event.timestamp_quality == "exact" for event in session.events)
    assert all(event.raw_kind == "sqlite.text-part" for event in session.events)
    assert (session.started_at, session.ended_at) == (
        session.events[0].timestamp,
        session.events[-1].timestamp,
    )
    assert session.metadata["retention_user_events"] == len(messages) - 1
    history = render_history(session, manifest.output)
    prompts = render_prompts(session, manifest.output)
    assert history.count("\u2014 peer-agent\n") == (3 if configured else 0)
    assert history.count("\u2014 user\n") == (
        len(humans) if configured else len(messages) - 1
    )
    assert "### 2026-02-03 04:05:06Z" in history
    assert (WAKE in prompts) is not configured
    assert messages[-1][1] not in prompts
    assert prompts.count("\n### ") == (len(humans) if configured else len(messages) - 1)
    for text in humans:
        assert text in prompts
    assert database.read_bytes() == original


@pytest.mark.parametrize("day_split", ["off", "hybrid", "all"])
@pytest.mark.parametrize("peer_only", [False, True])
def test_policy_change_rebuilds_managed_views_even_under_legacy_freeze(
    tmp_path,
    day_split,
    peer_only,
):
    database = tmp_path / "opencode.db"
    messages = [("user", WAKE)] + ([] if peer_only else [("user", HUMAN)])
    original = write_database(database, messages)
    data = configuration(tmp_path, indexes="owner")
    data["output"]["day_split"] = day_split
    data["output"]["compatibility"]["rule_version"] = "legacy-agent-markdown-frozen/v1"
    manifest = load_manifest(
        write_manifest(tmp_path / "manifest.json", data), environ={}
    )
    first, before, _ = run_pipeline(manifest, dry_run=False)
    assert first.status == "ok"
    inventory = scan_inventory(manifest)
    history = next(entry for entry in inventory.entries if entry.kind == "history")
    prompt = next(entry for entry in inventory.entries if entry.kind == "prompts")
    assert not history.grandfathered and not prompt.grandfathered
    assert WAKE in (manifest.output.repository_root / prompt.relative_path).read_text()
    corrected = replace(
        manifest,
        event_policy=replace(manifest.event_policy, peer_agent_prefixes=(MARKER,)),
    )

    report, after, plan = run_pipeline(corrected, dry_run=False)

    assert report.status == "ok"
    assert [session.identity for session in after.sessions] == [
        session.identity for session in before.sessions
    ]
    history_path = manifest.output.repository_root / history.relative_path
    prompt_path = manifest.output.repository_root / prompt.relative_path
    assert "\u2014 peer-agent\n" in history_path.read_text()
    assert WAKE in history_path.read_text()
    assert history.relative_path in {item.relative_path for item in plan.writes}
    if peer_only:
        assert not prompt_path.exists()
        assert {item.relative_path for item in plan.removals} == {prompt.relative_path}
        assert not any(
            entry.kind == "prompts" for entry in scan_inventory(corrected).entries
        )
        for index in (manifest.output.repository_root / "Prompts").rglob("*.md"):
            assert Path(prompt.relative_path).name not in index.read_text()
    else:
        assert HUMAN in prompt_path.read_text()
        assert WAKE not in prompt_path.read_text()
        assert not plan.removals
    again, _, _ = run_pipeline(corrected, dry_run=False)
    assert (again.write_count, again.removal_count) == (0, 0)
    assert database.read_bytes() == original


def test_policy_change_removes_peer_only_day_not_its_history(tmp_path):
    database = tmp_path / "opencode.db"
    original = write_database(
        database, [("user", HUMAN), ("user", WAKE)], step_ms=86_400_000
    )
    data = configuration(tmp_path)
    data["output"]["day_split"] = "hybrid"
    manifest = load_manifest(
        write_manifest(tmp_path / "manifest.json", data), environ={}
    )
    _, before, _ = run_pipeline(manifest, dry_run=False)
    corrected = replace(
        manifest,
        event_policy=replace(manifest.event_policy, peer_agent_prefixes=(MARKER,)),
    )

    _, after, plan = run_pipeline(corrected, dry_run=False)

    assert [session.day for session in after.sessions] == ["2026-02-03", "2026-02-04"]
    assert [session.identity for session in after.sessions] == [
        session.identity for session in before.sessions
    ]
    assert [item.identity for item in plan.removals] == [after.sessions[1].identity]
    inventory = scan_inventory(corrected)
    assert len([entry for entry in inventory.entries if entry.kind == "history"]) == 2
    assert [
        entry.identity for entry in inventory.entries if entry.kind == "prompts"
    ] == [after.sessions[0].identity]
    assert database.read_bytes() == original


@pytest.mark.parametrize("newest_authority", ["owner", "mirror"])
def test_declared_roots_deduplicate_by_complete_identity_not_authority(
    tmp_path, newest_authority
):
    data = configuration(tmp_path, ownership="aggregator", cleanup="aggregator")
    data["event_policy"]["peer_agent_prefixes"] = [MARKER]
    data["sources"] = []
    messages = [("user", WAKE), ("user", HUMAN), ("assistant", "Synthetic answer.")]
    originals = {}
    for source_id, authority, node, session_id, selected in (
        ("newer", newest_authority, "node-a", "session-example-12345678", messages),
        (
            "older",
            "mirror" if newest_authority == "owner" else "owner",
            "node-a",
            "session-example-12345678",
            messages[:2],
        ),
        ("additional-root", "owner", "node-a", "another-session-12345678", messages),
        ("other-node", "mirror", "node-b", "session-example-12345678", messages),
    ):
        database = tmp_path / f"{source_id}.db"
        originals[database] = write_database(database, selected, session_id=session_id)
        source = manifest_data(
            database,
            Path(data["output"]["repository_root"]),
            harness="opencode",
            source_id=source_id,
            node=node,
            discovery_mode="file",
            snapshot="sqlite-readonly",
        )["sources"][0]
        source["authority"] = authority
        data["sources"].append(source)
    manifest = load_manifest(
        write_manifest(tmp_path / "manifest.json", data), environ={}
    )

    report, snapshot, plan = run_pipeline(manifest, dry_run=True)

    assert report.status == "ok"
    assert len(snapshot.source_outcomes) == 4
    assert {session.identity for session in snapshot.sessions} == {
        ("opencode", "node-a", "session-example-12345678"),
        ("opencode", "node-a", "another-session-12345678"),
        ("opencode", "node-b", "session-example-12345678"),
    }
    shared = next(
        session
        for session in snapshot.sessions
        if session.identity
        == (
            "opencode",
            "node-a",
            "session-example-12345678",
        )
    )
    assert shared.source_ref.startswith("newer/")
    assert [event.role for event in shared.events] == [
        "peer-agent",
        "user",
        "assistant",
    ]
    assert len({item.relative_path for item in plan.writes}) == 6
    assert not snapshot.diagnostics
    _, reversed_snapshot, reversed_plan = run_pipeline(
        replace(manifest, sources=tuple(reversed(manifest.sources))),
        dry_run=True,
    )
    assert reversed_snapshot.sessions == snapshot.sessions
    assert reversed_plan == plan
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize("peer_only", [False, True])
def test_legacy_prompts_require_explicit_end_of_freeze_to_reclassify(
    tmp_path, peer_only
):
    database = tmp_path / "opencode.db"
    original = write_database(
        database, [("user", WAKE)] + ([] if peer_only else [("user", HUMAN)])
    )
    data = configuration(tmp_path)
    data["output"]["compatibility"]["rule_version"] = "legacy-agent-markdown-frozen/v1"
    manifest = load_manifest(
        write_manifest(tmp_path / "manifest.json", data), environ={}
    )
    _, _, generated = run_pipeline(manifest, dry_run=False)
    for item in generated.writes:
        # Reproduce the former writer's identity headers, with no managed marker.
        lines = item.content.decode().splitlines()
        lines = [
            line
            for line in lines
            if not line.startswith(
                (
                    "- Managed-By:",
                    "- Schema:",
                    "- View:",
                    "- Source:",
                )
            )
        ]
        lines = [
            line.replace("- Session:", "- Session ID:")
            for line in lines
            if item.kind == "history" or not line.startswith("- Session:")
        ]
        (manifest.output.repository_root / item.relative_path).write_text(
            "\n".join(lines) + "\n"
        )
    inventory = scan_inventory(manifest)
    assert all(entry.grandfathered for entry in inventory.entries)
    prompt = next(entry for entry in inventory.entries if entry.kind == "prompts")
    prompt_path = manifest.output.repository_root / prompt.relative_path
    frozen_bytes = prompt_path.read_bytes()
    corrected = replace(
        manifest,
        event_policy=replace(manifest.event_policy, peer_agent_prefixes=(MARKER,)),
    )

    _, _, frozen_plan = run_pipeline(corrected, dry_run=False)

    assert [item.kind for item in frozen_plan.writes] == ["history"]
    assert prompt_path.read_bytes() == frozen_bytes
    assert not frozen_plan.removals
    adopted = replace(
        corrected,
        output=replace(corrected.output, compatibility_rule="legacy-agent-markdown/v1"),
    )
    _, _, plan = run_pipeline(adopted, dry_run=False)
    if peer_only:
        assert not prompt_path.exists()
        assert {item.relative_path for item in plan.removals} == {prompt.relative_path}
    else:
        assert {item.relative_path for item in plan.writes} == {prompt.relative_path}
        assert HUMAN in prompt_path.read_text()
        assert WAKE not in prompt_path.read_text()
    again, _, _ = run_pipeline(adopted, dry_run=False)
    assert (again.write_count, again.removal_count) == (0, 0)
    assert database.read_bytes() == original


def test_unpaired_legacy_prompt_is_preserved_not_guessed_after_policy_change(tmp_path):
    database = tmp_path / "opencode.db"
    original = write_database(database, [("user", WAKE)])
    data = configuration(tmp_path)
    data["output"]["compatibility"]["rule_version"] = "legacy-agent-markdown/v1"
    manifest = load_manifest(
        write_manifest(tmp_path / "manifest.json", data), environ={}
    )
    prompt_path = manifest.output.repository_root / "Prompts" / "unpaired.md"
    prompt_path.parent.mkdir()
    legacy = (
        "# Legacy prompt\n\n- Tool: opencode\n- Host: node-a\n\n---\n\n"
        f"### 2026-02-03 04:05:06Z\n\n{WAKE}\n\n---\n"
    ).encode()
    prompt_path.write_bytes(legacy)
    (entry,) = scan_inventory(manifest).entries
    assert entry.grandfathered and entry.identity is None
    corrected = replace(
        manifest,
        event_policy=replace(manifest.event_policy, peer_agent_prefixes=(MARKER,)),
    )

    report, snapshot, plan = run_pipeline(corrected, dry_run=False)

    assert report.status == "ok"
    assert len(snapshot.sessions) == 1
    assert [event.role for event in snapshot.sessions[0].events] == ["peer-agent"]
    assert [item.kind for item in plan.writes] == ["history"]
    assert not plan.removals
    assert prompt_path.read_bytes() == legacy
    assert database.read_bytes() == original
