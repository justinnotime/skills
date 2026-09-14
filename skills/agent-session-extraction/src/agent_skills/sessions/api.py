"""Stable public API for scripts, schedulers, and shadow adapters."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from .manifest import DAY_SPLIT_MODES, Manifest, ManifestError, load_manifest
from .model import Diagnostic, ReconcileReport, RunReport
from .pipeline import (
    PipelineError,
    decode_source_snapshots,
    evaluate_pipeline,
    extract_sessions,
    run_pipeline,
)
from .reconcile import clear_failure_marker, write_failure_marker
from .redact import Redactor
from .sources import (
    SourceAccessError,
    discovery_roots,
    session_metadata_source,
    validate_configured_path,
)

from .telemetry import decode_telemetry


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    if environ is not None:
        return environ
    # HOME is the only ambient value understood by manifest v1.
    return {"HOME": os.environ.get("HOME", "")}


def _with_day_split(manifest: Manifest, day_split: str | None) -> Manifest:
    """Apply an optional caller override of ``output.day_split``."""
    if day_split is None:
        return manifest
    if day_split not in DAY_SPLIT_MODES:
        raise ManifestError("day split override must be one of: off, hybrid, all")
    return replace(manifest, output=replace(manifest.output, day_split=day_split))


def run(
    manifest_path: str | os.PathLike[str],
    *,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    failure_marker: Path | None = None,
    git_worktree_destination: Path | None = None,
    output_root: Path | None = None,
    day_split: str | None = None,
) -> RunReport:
    manifest = load_manifest(manifest_path, environ=_environment(environ))
    manifest = _with_day_split(manifest, day_split)
    if output_root is not None:
        if not output_root.is_absolute():
            raise ManifestError("output root override must be absolute")
        manifest = replace(
            manifest,
            output=replace(manifest.output, repository_root=output_root),
        )
    try:
        report, _snapshot, _plan = run_pipeline(
            manifest,
            dry_run=dry_run,
            git_worktree_destination=git_worktree_destination,
        )
    except PipelineError as exc:
        if failure_marker is not None and not dry_run:
            write_failure_marker(
                failure_marker,
                ReconcileReport(
                    False,
                    exc.checks,
                    exc.diagnostics or (Diagnostic(exc.code, "pipeline"),),
                ),
            )
        raise
    if failure_marker is not None and not dry_run:
        clear_failure_marker(failure_marker)
    return report


def doctor(
    manifest_path: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Validate capabilities and configured roots without decoding transcripts."""
    manifest = load_manifest(manifest_path, environ=_environment(environ))
    Redactor.from_spec(manifest.redaction)
    from .harnesses.base import decoder_for
    from .pipeline import _load_decoders

    _load_decoders()
    sources = []
    ok = True
    for source in manifest.sources:
        if not source.enabled:
            sources.append({"id": source.source_id, "status": "disabled"})
            continue
        try:
            root = validate_configured_path(source)
            if source.discovery.directories:
                discovery_roots(source, root)
            metadata_source = session_metadata_source(source)
            if metadata_source is not None:
                validate_configured_path(metadata_source)
            capabilities = decoder_for(source.harness).capabilities()
            sources.append(
                {
                    "id": source.source_id,
                    "status": "ok",
                    "capabilities": list(capabilities),
                }
            )
        except (SourceAccessError, OSError, ValueError):
            ok = False
            sources.append({"id": source.source_id, "status": "invalid"})
    return {
        "schema_version": "agent-session-doctor-report/v1",
        "status": "ok" if ok else "failed",
        "sources": sources,
    }


def reconcile(
    manifest_path: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
    failure_marker: Path | None = None,
    day_split: str | None = None,
) -> ReconcileReport:
    manifest = load_manifest(manifest_path, environ=_environment(environ))
    manifest = _with_day_split(manifest, day_split)
    _snapshot, _inventory, _plan, report, _redactor = evaluate_pipeline(manifest)
    if failure_marker is not None:
        if report.ok:
            clear_failure_marker(failure_marker)
        else:
            write_failure_marker(failure_marker, report)
    return report


__all__ = [
    "Manifest",
    "PipelineError",
    "decode_source_snapshots",
    "decode_telemetry",
    "doctor",
    "extract_sessions",
    "load_manifest",
    "reconcile",
    "run",
]
