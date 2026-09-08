#!/usr/bin/env python3
"""Local, read-only Codex usage accounting. No network or model calls."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
DATA = Path.home() / '.local/share/codex-cost'
KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
        'output_tokens', 'reasoning_output_tokens')


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(path)


def valid_id(value):
    if not value or not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}', value):
        raise ValueError('a valid Codex thread ID is required')
    return value


def transcript(thread):
    valid_id(thread)
    with sqlite3.connect(f'file:{CODEX_DIR}/state_5.sqlite?mode=ro', uri=True) as db:
        row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (thread,)).fetchone()
    if not row:
        raise ValueError('thread is not present in the local Codex database')
    return Path(row[0])


def records(path):
    with path.open() as f:
        for line in f:
            if not line.endswith('\n'):
                break  # The active writer has not finished the final line.
            yield json.loads(line)


def normalize_tier(tier):
    return {'priority': 'fast', 'default': 'standard'}.get(tier, tier)


def price(usage, model, tier, prices):
    catalog = prices['models'].get(model, {})
    tier = normalize_tier(tier)
    choices = [tier] if tier else ['standard', 'fast']
    totals = []
    for choice in choices:
        rates = catalog.get(choice)
        if not rates:
            return None
        rates = rates['long' if usage['input_tokens'] > prices['long_context_threshold'] else 'short']
        ordinary = usage['input_tokens'] - usage['cached_input_tokens'] - usage['cache_write_input_tokens']
        if ordinary < 0 or any(usage[k] < 0 for k in KEYS):
            raise ValueError('invalid input/cache token categories')
        counts = (ordinary, usage['cached_input_tokens'], usage['cache_write_input_tokens'], usage['output_tokens'])
        if any(count and rate is None for count, rate in zip(counts, rates)):
            return None
        totals.append(sum(count * (rate or 0) for count, rate in zip(counts, rates)) / 1_000_000)
    return [min(totals), max(totals)]


def analyze(events, prices, settings=None):
    settings = settings or {}
    turns = {}
    current = 'legacy-unknown'
    model = None
    tier = None
    previous = dict.fromkeys(KEYS, 0)
    seen = set()
    for event in events:
        p = event.get('payload', {})
        kind = event.get('type')
        typ = p.get('type') if kind == 'event_msg' else kind
        if typ in ('task_started', 'turn_context'):
            current = p.get('turn_id') or current
        if typ == 'turn_context':
            model = p.get('model')
            tier = p.get('service_tier')
        if typ in ('task_complete', 'turn_aborted') and current in turns:
            turns[current]['status'] = 'completed' if typ == 'task_complete' else 'interrupted'
        if typ not in ('token_usage_record', 'token_count'):
            continue
        turn_id = p.get('turn_id') or current
        t = turns.setdefault(turn_id, {'turn_id': turn_id, 'started_at': event.get('timestamp'),
                                     'status': 'in progress', 'canonical': [], 'legacy': []})
        if typ == 'token_usage_record':
            response = p.get('response_id')
            if not response:
                raise ValueError('usage record is missing its response ID')
            if response in seen:
                continue
            seen.add(response)
            usage = {k: p['usage'].get(k, 0) for k in KEYS}
        else:
            info = p.get('info')
            if not info or not info.get('total_token_usage'):
                continue
            total = {k: info['total_token_usage'].get(k, 0) for k in KEYS}
            delta = {k: total[k] - previous[k] for k in KEYS}
            previous = total
            if not any(delta.values()):
                continue
            if any(v < 0 for v in delta.values()):
                raise ValueError('legacy cumulative counters decreased; totals are unavailable')
            usage = delta
        snapshot = settings.get(turn_id, {})
        selected_tier = tier or snapshot.get('tier')
        selected_model = p.get('model') or model or snapshot.get('model')
        t['canonical' if typ == 'token_usage_record' else 'legacy'].append(
            (usage, selected_model, selected_tier))
    output = []
    for t in turns.values():
        calls = t.pop('canonical') or t.pop('legacy')
        t.pop('legacy', None)
        if not calls:
            continue
        t.update({k: sum(u[k] for u, _, _ in calls) for k in KEYS})
        t['calls'] = len(calls)
        t['models'] = sorted({m or 'unknown' for _, m, _ in calls})
        t['tiers'] = sorted({normalize_tier(s) or 'unknown (Standard-Fast range)' for _, _, s in calls})
        costs = [price(u, m, s, prices) for u, m, s in calls]
        t['unpriced_calls'] = sum(v is None for v in costs)
        t['usd'] = None if t['unpriced_calls'] else [sum(v[i] for v in costs) for i in (0, 1)]
        t['cache_hit_percent'] = 100 * t['cached_input_tokens'] / t['input_tokens'] if t['input_tokens'] else None
        output.append(t)
    total = {k: sum(t[k] for t in output) for k in KEYS}
    total['calls'] = sum(t['calls'] for t in output)
    total['unpriced_calls'] = sum(t['unpriced_calls'] for t in output)
    total['usd'] = None if total['unpriced_calls'] else [sum(t['usd'][i] for t in output) for i in (0, 1)]
    total['cache_hit_percent'] = 100 * total['cached_input_tokens'] / total['input_tokens'] if total['input_tokens'] else None
    return {'turns': output, 'total': total, 'price_source': prices['source'],
            'prices_retrieved_at': prices['retrieved_at'],
            'basis': 'API list-price estimate; configured tier when recorded, otherwise Standard-Fast range. Excludes tool fees, discounts, taxes and other threads.'}


def report(thread):
    path = transcript(thread)
    settings = read_json(DATA / 'turn-settings' / f'{valid_id(thread)}.json', {})
    result = analyze(records(path), read_json(ROOT / 'prices.json'), settings)
    result['thread_id'] = thread
    return result


def money(value):
    if value is None:
        return 'unavailable'
    return f'~${value[0]:.4f}' if abs(value[0] - value[1]) < 1e-9 else f'~${value[0]:.4f}-${value[1]:.4f}'


def pct(value):
    return 'n/a' if value is None else f'{value:.1f}%'


def summary(r):
    if not r['turns']:
        return 'Codex cost: waiting for usage'
    last = r['turns'][-1]
    return (f"Turn {money(last['usd'])} | Total {money(r['total']['usd'])} | "
            f"Cache {pct(last['cache_hit_percent'])}/{pct(r['total']['cache_hit_percent'])} turn/all | "
            f"{r['total']['calls']} calls")



def hook(payload):
    thread = payload.get('session_id') or os.environ.get('CODEX_THREAD_ID')
    valid_id(thread)
    event = payload.get('hook_event_name')
    if event == 'UserPromptSubmit':
        turn = payload.get('turn_id')
        if not turn:
            for e in records(transcript(thread)):
                p = e.get('payload', {})
                if p.get('type') == 'task_started':
                    turn = p.get('turn_id')
        if turn:
            config = tomllib.loads((CODEX_DIR / 'config.toml').read_text())
            path = DATA / 'turn-settings' / f'{thread}.json'
            settings = read_json(path, {})
            settings.setdefault(turn, {'tier': config.get('service_tier', 'standard'),
                                      'model': payload.get('model'),
                                      'basis': 'user config captured at prompt submission',
                                      'recorded_at': dt.datetime.now(dt.timezone.utc).isoformat()})
            save_json(path, settings)
    if event in ('Stop', 'Interrupt'):
        return {'systemMessage': summary(report(thread)) + ' | API list-price estimate'}
    return {}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['turns', 'status', 'hook'], nargs='?', default='turns')
    p.add_argument('--thread', default=os.environ.get('CODEX_THREAD_ID'))
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    if a.command == 'hook':
        try:
            print(json.dumps(hook(json.load(sys.stdin))))
        except Exception as exc:
            print(json.dumps({'systemMessage': f'Codex cost unavailable: {type(exc).__name__}'}))
        return
    r = report(a.thread)
    if a.json:
        print(json.dumps(r, indent=2))
    elif a.command == 'status':
        print(summary(r))
    else:
        print('API list-price estimates in USD; Cache = cached input / all input tokens.')
        print('Turn  USD estimate             Cache     Input       Read      Write     Output  Calls  State')
        for n, t in enumerate(r['turns'], 1):
            print(f"{n:>4}  {money(t['usd']):<23} {pct(t['cache_hit_percent']):>6} "
                  f"{t['input_tokens']:>10,} {t['cached_input_tokens']:>10,} "
                  f"{t['cache_write_input_tokens']:>10,} {t['output_tokens']:>10,} "
                  f"{t['calls']:>5}  {t['status']}")
        print(summary(r))
        print(r['basis'])
        print('Pricing:', r['price_source'], 'retrieved', r['prices_retrieved_at'])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'Codex cost unavailable: {type(exc).__name__}')
        sys.exit(1)
