import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import os
import sqlite3
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cost', ROOT / 'scripts/cost.py')
cost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cost)
PRICES = json.loads((ROOT / 'prices.json').read_text())


def usage(i=100, read=60, write=30, out=20, reasoning=10):
    return dict(zip(cost.KEYS, (i, read, write, out, reasoning)))


def context(turn='turn-one', model='gpt-6-astra', tier='standard'):
    return {'type': 'turn_context', 'payload': {'turn_id': turn, 'model': model, 'service_tier': tier}}


def event(response='one', turn='turn-one', u=None):
    return {'type': 'token_usage_record', 'payload': {'turn_id': turn, 'response_id': response, 'usage': u or usage()}}


class Accounting(unittest.TestCase):
    def test_disjoint_input_and_reasoning_not_double_charged(self):
        # 10*10 + 60*1 + 30*12.5 + 20*50 = 1535 microdollars.
        self.assertEqual(cost.price(usage(), 'gpt-6-astra', 'standard', PRICES), [.001535, .001535])

    def test_fast_and_unknown_tier(self):
        self.assertEqual(cost.price(usage(), 'gpt-6-astra', 'priority', PRICES), [.00307, .00307])
        self.assertEqual(cost.price(usage(), 'gpt-6-astra', None, PRICES), [.001535, .00307])

    def test_threshold_per_request(self):
        low = cost.price(usage(i=272000, read=0, write=0, out=20), 'gpt-6-astra', 'standard', PRICES)[0]
        high = cost.price(usage(i=272001, read=0, write=0, out=20), 'gpt-6-astra', 'standard', PRICES)[0]
        self.assertAlmostEqual(low, 2.721)
        self.assertAlmostEqual(high, 5.44152)

    def test_dedup_canonical_and_shadow_stream(self):
        shadow = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': usage()}}}
        r = cost.analyze([context(), event(), event(), shadow, shadow], PRICES)
        self.assertEqual(r['total']['calls'], 1)
        self.assertEqual(r['total']['input_tokens'], 100)

    def test_turn_boundary_model_and_weighted_cache(self):
        r = cost.analyze([context(), event(), context('turn-two', 'gpt-5.6-sol'),
                          event('two', 'turn-two', usage(i=900, read=0, write=0)),
                          {'type': 'event_msg', 'payload': {'type': 'task_complete'}}], PRICES)
        self.assertEqual(len(r['turns']), 2)
        self.assertEqual(r['total']['cache_hit_percent'], 6)
        self.assertEqual(r['turns'][1]['models'], ['gpt-5.6-sol'])
        self.assertEqual(r['turns'][1]['status'], 'completed')

    def test_unknown_price_is_not_zero(self):
        r = cost.analyze([context(model='unknown'), event()], PRICES)
        self.assertIsNone(r['total']['usd'])
        self.assertEqual(r['total']['unpriced_calls'], 1)

    def test_invalid_categories_fail(self):
        with self.assertRaises(ValueError):
            cost.price(usage(i=50), 'gpt-6-astra', 'standard', PRICES)

    def test_legacy_cumulative_deltas_and_rate_limit_repeat(self):
        def old(u):
            return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': u}}}
        first = usage()
        second = {k: v*2 for k,v in first.items()}
        r = cost.analyze([context(), old(first), old(first), context('two'), old(second)], PRICES)
        self.assertEqual(r['total']['calls'], 2)
        self.assertEqual(r['total']['input_tokens'], 200)
        self.assertEqual(r['turns'][1]['input_tokens'], 100)

    def test_negative_legacy_counter_fails(self):
        def old(u):
            return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': u}}}
        with self.assertRaises(ValueError):
            cost.analyze([context(), old(usage()), old(usage(i=90))], PRICES)

    def test_unfinished_line_ignored_but_corrupt_complete_line_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.jsonl'
            path.write_text(json.dumps(context()) + '\n{"partial":')
            self.assertEqual(len(list(cost.records(path))), 1)
            path.write_text('{bad}\n')
            with self.assertRaises(json.JSONDecodeError):
                list(cost.records(path))

    def test_explicit_tier_beats_snapshot(self):
        r = cost.analyze([context(), event()], PRICES, {'turn-one': {'tier': 'fast'}})
        self.assertEqual(r['total']['usd'], [.001535, .001535])

    def test_two_windows_report_their_own_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            with sqlite3.connect(base / 'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
                for thread, count in [('thread-left', 100000), ('thread-right', 200000)]:
                    path = base / (thread + '.jsonl')
                    rows = [context(thread), event(thread, thread, usage(i=count, read=0, write=0, out=0, reasoning=0))]
                    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                    db.execute('INSERT INTO threads VALUES (?, ?)', (thread, str(path)))
            env = dict(os.environ, CODEX_HOME=folder, CODEX_THREAD_ID='wrong-environment-thread')
            for pane, thread, expected in [('%101', 'thread-left', '~$1.0000'),
                                            ('%202', 'thread-right', '~$2.0000'),
                                            ('%101', 'thread-left', '~$1.0000')]:
                env['TMUX_PANE'] = pane
                output = subprocess.check_output(
                    [os.sys.executable, str(ROOT/'scripts/cost.py'), 'hook'],
                    input=json.dumps({'hook_event_name':'Stop', 'session_id':thread}),
                    text=True, env=env)
                message = json.loads(output)['systemMessage']
                self.assertIn('Turn '+expected+' | Total '+expected, message)


if __name__ == '__main__':
    unittest.main()
