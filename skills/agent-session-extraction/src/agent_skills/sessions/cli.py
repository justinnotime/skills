"""Transcript-safe command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .api import doctor, reconcile, run
from .manifest import ManifestError
from .pipeline import PipelineError
from .redact import RedactionError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-session-extraction")
    commands = parser.add_subparsers(dest="command", required=True)

    extract = commands.add_parser(
        "extract", help="run the deterministic extraction pipeline"
    )
    extract.add_argument("--manifest", required=True)
    extract.add_argument("--dry-run", action="store_true")
    extract.add_argument("--failure-marker", type=Path)
    extract.add_argument("--prepare-worktree", type=Path)
    extract.add_argument("--worktree-ref", help="prepare from this commit before reading output inventory")
    extract.add_argument("--output-root", type=Path)
    extract.add_argument(
        "--day-split",
        choices=("off", "hybrid", "all"),
        help="override output.day_split for this run",
    )

    check = commands.add_parser(
        "doctor", help="check manifest, paths, and decoder capabilities"
    )
    check.add_argument("--manifest", required=True)

    compare = commands.add_parser(
        "reconcile", help="decode and compare sources with planned output"
    )
    compare.add_argument("--manifest", required=True)
    compare.add_argument("--failure-marker", type=Path)
    compare.add_argument("--day-split", choices=("off", "hybrid", "all"))
    usage = commands.add_parser(
        "analyze-usage", help="write private usage and activity observations"
    )
    usage.add_argument("--manifest", required=True)
    usage.add_argument("--start", required=True)
    usage.add_argument("--end", required=True)
    usage.add_argument("--output", type=Path, required=True)
    usage.add_argument("--config", type=Path)
    statistics = commands.add_parser(
        "summarize-usage", help="summarize local usage evidence"
    )
    statistics.add_argument("--input", type=Path, required=True)
    statistics.add_argument("--output", type=Path, required=True)
    statistics.add_argument("--bootstrap-seed", type=int, default=0)
    statistics.add_argument("--bootstrap-resamples", type=int, default=1000)
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "summarize-usage":
            from .usage_statistics import summarize_file

            _emit(
                summarize_file(
                    args.input,
                    args.output,
                    seed=args.bootstrap_seed,
                    resamples=args.bootstrap_resamples,
                )
            )
            return 0
        if args.command == "analyze-usage":
            from .usage_analysis import run_analysis

            config = json.loads(args.config.read_text()) if args.config else {}
            _emit(
                run_analysis(
                    args.manifest,
                    args.output,
                    start=args.start,
                    end=args.end,
                    config=config,
                )
            )
            return 0
        if args.command == "extract":
            report = run(
                args.manifest,
                dry_run=args.dry_run,
                failure_marker=args.failure_marker,
                git_worktree_destination=args.prepare_worktree,
                git_worktree_ref=args.worktree_ref,
                output_root=args.output_root,
                day_split=args.day_split,
            )
            _emit(asdict(report))
            return 0
        if args.command == "doctor":
            report = doctor(args.manifest)
            _emit(report)
            return 0 if report["status"] == "ok" else 1
        report = reconcile(
            args.manifest, failure_marker=args.failure_marker, day_split=args.day_split
        )
        _emit(
            {
                "status": "ok" if report.ok else "failed",
                "checks": dict(report.checks),
                "diagnostics": [asdict(item) for item in report.diagnostics],
            }
        )
        return 0 if report.ok else 1
    except (ManifestError, PipelineError, RedactionError) as exc:
        code = (
            exc.code if isinstance(exc, PipelineError) else type(exc).__name__.upper()
        )
        _emit({"status": "failed", "code": code})
        return 2
    # The CLI deliberately suppresses unexpected exception text because a
    # decoder exception may contain an absolute path or transcript fragment.
    except Exception:  # noqa: BLE001
        _emit({"status": "failed", "code": "UNEXPECTED_FAILURE"})
        return 2


if __name__ == "__main__":
    sys.exit(main())
