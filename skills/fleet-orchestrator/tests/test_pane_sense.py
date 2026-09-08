"""Tests for minimal pane lookup and the explicit busy indicator. No tmux."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import pane_sense  # noqa: E402


def load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / script)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


TMUX_SEND = load("agent-tmux-send.py", "agent_tmux_send_for_pane_tests")


class DetectBusyTests(unittest.TestCase):
    def test_interrupt_hint_is_busy(self):
        self.assertTrue(pane_sense.detect_busy(
            "some output\nWorking (1m 10s - Esc to interrupt)\n"))

    def test_notes_working_cwd_is_not_busy(self):
        self.assertFalse(pane_sense.detect_busy(
            "gpt-5.6-sol ultra · ~/src/example-working-tree · Main [default]\n> "))

    def test_busy_marker_only_counts_near_the_prompt(self):
        old_busy = "Working (3m - Esc to interrupt)"
        filler = "\n".join(
            f"line {i}" for i in range(pane_sense.BUSY_TAIL_LINES + 5))
        self.assertFalse(pane_sense.detect_busy(f"{old_busy}\n{filler}\n> "))

    def test_capture_reads_only_the_busy_indicator_window(self):
        with mock.patch.object(pane_sense, "tmux_out",
                               return_value="pane text") as tmux_out:
            self.assertEqual(pane_sense.capture("%17"), "pane text")
        tmux_out.assert_called_once_with([
            "capture-pane", "-p", "-t", "%17", "-S",
            f"-{pane_sense.BUSY_TAIL_LINES}",
        ])


class PaneParsingTests(unittest.TestCase):
    def test_grouped_sessions_dedupe_and_all_harnesses_count(self):
        rows = "\n".join([
            "%30\t0:16.0\tcodex\t0",
            "%30\t0-33:16.0\tcodex\t0",
            "%30\t0-34:16.0\tcodex\t0",
            "%31\t0:17.0\tcodex\t0",
            "%12\t0:5.0\tclaude\t0",
            "%13\t0:4.0\topencode\t0",
            "%99\t0:2.0\tzsh\t0",
            "%77\t0:5.0\tcodex\t1",
        ])
        self.assertEqual(pane_sense.parse_agent_pane_rows(rows),
                         [("%30", "0:16.0"), ("%31", "0:17.0"),
                          ("%12", "0:5.0"), ("%13", "0:4.0")])

    def test_malformed_rows_skipped(self):
        self.assertEqual(
            pane_sense.parse_agent_pane_rows(
                "garbage line\n%1 only-two-fields"), [])

    def test_pane_for_window_can_use_one_snapshot(self):
        panes = [("%21", "claude-migration:2.0"),
                 ("%27", "claude-migration:15.0")]
        self.assertEqual(pane_sense.pane_for_window("15", panes),
                         ("%27", "claude-migration:15.0"))
        self.assertIsNone(pane_sense.pane_for_window("20", panes))

    def test_pane_for_window_never_resolves_through_tview_mirrors(self):
        panes = [("%90", "tview-example-pts-42:5.0"),
                 ("%14", "0:5.0")]
        self.assertEqual(pane_sense.pane_for_window("5", panes),
                         ("%14", "0:5.0"))
        self.assertIsNone(pane_sense.pane_for_window(
            "5", [("%90", "tview-example-pts-42:5.0")]))


class NamedServerSelectionTests(unittest.TestCase):
    def test_machine_config_reaches_agent_tmux_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "state" / "fleet-orchestrator" / "tmux-server"
            cfg.parent.mkdir(parents=True)
            cfg.write_text("tmux37\n")
            config = root / "config.json"
            config.write_text("{}")
            with mock.patch.dict(
                    os.environ, {"NOTES_RUNTIME_DIR": tmp,
                                 "FLEET_ORCHESTRATOR_CONFIG": str(config)}, clear=False):
                os.environ.pop("NW_TMUX_SERVER", None)
                self.assertEqual(TMUX_SEND.tmux_base_cmd(),
                                 ["tmux", "-L", "tmux37"])


class NudgeContractTests(unittest.TestCase):
    def test_no_authority_bearing_nudge_exists(self):
        self.assertNotIn("authorize", TMUX_SEND.NUDGES)

    def test_unknown_nudge_key_rejected(self):
        with self.assertRaises(ValueError):
            TMUX_SEND.send("%0", "", nudge_key="approve-everything")


if __name__ == "__main__":
    unittest.main()
