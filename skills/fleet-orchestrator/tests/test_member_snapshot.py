"""Agent Bus membership has no persistent ORC copy or explicit sync step."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'lib'))
import workplane as wp


MEMBER = {
    'agent_id': 'agent-1', 'handle': 'test/worker', 'aliases': ['worker'],
    'host': 'remote', 'tmux': 'tmux=team:1.0',
    'status': 'active', 'addressable': True,
}


class MemberSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        patcher = mock.patch.dict(os.environ, {'DISPATCH_LEDGER_DB': str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self, members, *, readonly=False):
        with mock.patch.object(wp, 'read_members', return_value=members) as source:
            conn = (wp.connect_readonly() if readonly else wp.connect_writable())
            self.assertEqual(source.call_count, 1)
        self.addCleanup(conn.close)
        return conn

    def test_next_connection_observes_bus_change_without_sync_or_ledger_write(self):
        initial = self.connect([MEMBER])
        task_id = wp.insert_task(initial, recipient='worker', subject='preserved task',
                                 check_cmd='true')
        initial.commit()
        self.assertEqual(wp.resolve_recipient(initial, 'worker')['agent_id'], 'agent-1')
        self.assertIsNone(initial.execute(
            "SELECT name FROM main.sqlite_master WHERE name='seat'").fetchone())
        task_before = dict(wp.fetch(initial, task_id))
        initial.close()

        empty = self.connect([], readonly=True)
        self.assertTrue(wp.members_available(empty))
        self.assertEqual(empty.execute('SELECT COUNT(*) FROM seat').fetchone()[0], 0)
        self.assertIsNone(wp.resolve_recipient(empty, 'worker')['agent_id'])
        self.assertEqual(dict(wp.fetch(empty, task_id)), task_before)
        with self.assertRaises(sqlite3.OperationalError):
            empty.execute("UPDATE dispatch SET subject='forbidden'")

    def test_unavailable_has_no_stale_recipient_but_is_not_empty_success(self):
        old = self.connect([MEMBER])
        with old:
            old.execute("INSERT INTO role_assignment(role,agent_id,granted_by,granted_ms)"
                        " VALUES('reviewer-pool','agent-1','test',1)")
        old.close()
        failed = self.connect(None)
        self.assertFalse(wp.members_available(failed))
        self.assertIsNone(wp.pool_pick(failed, 'reviewer-pool'))
        self.assertIsNone(wp.resolve_recipient(failed, 'worker')['agent_id'])
        self.assertEqual(failed.execute('SELECT COUNT(*) FROM seat').fetchone()[0], 0)
        self.assertEqual(failed.execute('SELECT COUNT(*) FROM role_assignment').fetchone()[0], 1)

    def test_readonly_ignores_legacy_persistent_members(self):
        with sqlite3.connect(self.path) as legacy:
            legacy.execute('CREATE TABLE seat(agent_id TEXT)')
            legacy.execute("INSERT INTO seat VALUES ('stale')")
        readonly = self.connect([], readonly=True)
        self.assertEqual(readonly.execute('SELECT COUNT(*) FROM seat').fetchone()[0], 0)
        self.assertEqual(readonly.execute('SELECT COUNT(*) FROM main.seat').fetchone()[0], 1)
        readonly.close()
        writable = self.connect([])
        self.assertIsNone(writable.execute(
            "SELECT name FROM main.sqlite_master WHERE name='seat'").fetchone())

    def test_exact_pane_survives_resolution_with_live_window_position(self):
        member = {**MEMBER, 'host': socket.gethostname().split('.', 1)[0],
                  'pane_id': '%82', 'tmux': 'tmux=renamed:9.1',
                  'terminal_presence': 'present'}
        conn = self.connect([member])
        target = wp.resolve_recipient(conn, 'worker')
        self.assertEqual((target['window'], target['pane_id']), ('9', '%82'))
        self.assertEqual(target['terminal_presence'], 'present')

    def test_exact_pane_never_selects_a_neighbour_or_unknown_location(self):
        spec = importlib.util.spec_from_file_location(
            'orc_member_snapshot_test', ROOT / 'scripts' / 'fleet-orchestrator.py')
        orc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(orc)
        panes = [('%81', 'team:9.0'), ('%82', 'team:9.1')]
        member = {'pane_id': '%82', 'window': '9', 'terminal_presence': 'present'}
        self.assertEqual(orc._registered_pane(member, panes), panes[1])
        self.assertIsNone(orc._registered_pane(member, panes[:1]))
        for state in ('unknown', 'absent'):
            self.assertIsNone(orc._registered_pane({**member, 'terminal_presence': state}, panes))
        conn = self.connect([])
        unknown = {**MEMBER, 'mode': 'watch', 'pane_id': '%82',
                   'terminal_presence': 'unknown'}
        retire = mock.Mock()
        orc.tick_seat_liveness(conn, False, members=[unknown], panes=panes,
                              hostnames={'remote'}, watcher_alive=lambda _: False,
                              retire=retire, now_ms=10)
        self.assertFalse(retire.called)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM seat_watch').fetchone()[0], 0)

    def test_first_task_write_binds_local_history_but_readonly_does_not(self):
        module = mock.Mock()
        local = {'NW_FLEET': 'alpha', 'NW_FLEET_PRIMARY_SESSION': 'alpha',
                 'NW_FLEET_PROFILE_PATH': '', 'AGENT_BUS_TRANSPORT': 'local'}
        with mock.patch.dict(os.environ, local), mock.patch.object(
                wp.importlib.util, 'spec_from_file_location') as spec, mock.patch.object(
                wp.importlib.util, 'module_from_spec', return_value=module):
            conn = self.connect([])
            module.bind_local_session.assert_called_once_with('alpha', os.environ)
            conn.close()
            module.bind_local_session.reset_mock()
            self.connect([], readonly=True)
            self.assertFalse(module.bind_local_session.called)
            self.assertEqual(spec.call_count, 1)

    def test_binding_failure_prevents_task_database_creation(self):
        with mock.patch.object(wp, '_bind_local_history', side_effect=ValueError('cannot bind history')):
            with self.assertRaisesRegex(ValueError, 'cannot bind history'):
                wp.connect_writable()
        self.assertFalse(self.path.exists())

    def test_nonlocal_and_explicit_profiles_do_not_bind_automatic_history(self):
        base = {'NW_FLEET': 'alpha', 'NW_FLEET_PRIMARY_SESSION': 'alpha',
                'NW_FLEET_PROFILE_PATH': '', 'AGENT_BUS_TRANSPORT': 'local'}
        for change in ({'NW_FLEET': ''}, {'NW_FLEET': 'default'},
                       {'NW_FLEET_PROFILE_PATH': '/explicit/profile.json'},
                       {'AGENT_BUS_TRANSPORT': 'matrix'},
                       {'NW_FLEET_PRIMARY_SESSION': ''}):
            with self.subTest(change=change), mock.patch.dict(os.environ, {**base, **change}), mock.patch.object(
                    wp.importlib.util, 'spec_from_file_location') as loader:
                wp._bind_local_history()
                self.assertFalse(loader.called)

    def test_source_output_empty_is_success_and_malformed_is_unavailable(self):
        for output, expected in [('', []), ('\n ', []),
                                 (json.dumps(MEMBER), [MEMBER]),
                                 ('not-json', None), ('[]', None),
                                 ('{"agent_id":"broken", "aliases":42}', None),
                                 (json.dumps(MEMBER) + '\nnot-json', None),
                                 (json.dumps(MEMBER) + '\n' + json.dumps(MEMBER), None)]:
            with self.subTest(output=output), mock.patch.object(
                    wp.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        ['bus', 'members'], 0, stdout=output, stderr='')):
                self.assertEqual(wp.read_members(), expected)

    def test_source_timeout_and_failure_are_unavailable(self):
        with mock.patch.object(wp.subprocess, 'run', side_effect=subprocess.TimeoutExpired('bus', 1)):
            self.assertIsNone(wp.read_members(timeout=1))
        with mock.patch.object(wp.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['bus', 'members'], 1, stdout='', stderr='unavailable')):
            self.assertIsNone(wp.read_members())


if __name__ == '__main__':
    unittest.main()
