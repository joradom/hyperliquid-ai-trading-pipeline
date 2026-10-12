"""Summarize an offline REST run without modifying its original evidence."""
from __future__ import annotations

import argparse
from collections import Counter, deque
import json
import math
from pathlib import Path


def distribution(values):
    values = sorted(values)
    return {'n': len(values), **{name: values[math.ceil(len(values)*q)-1] if values else None
                                for name, q in [('p50', .5), ('p95', .95), ('max', 1)]}}


def analyze(directory):
    manifest = json.loads((directory/'manifest.json').read_text())
    summary = json.loads((directory/'summary.json').read_text())
    events = [json.loads(line) for line in (directory/'events.jsonl').read_text().splitlines()]
    rounds = {e['round']: e for e in events if e['kind'] == 'research'}
    groups = {}
    for number, row in rounds.items():
        key = f"{row['mode']}:pool{row['pool']}:incremental{row['incremental']}:capacity{row['max_admitted']}:fault{row['fault']}"
        group = groups.setdefault(key, {'rounds': 0, 'capture_to_model': [], 'capture': [],
                                         'round_seconds': [], 'captures': 0, 'model_calls': 0,
                                         'review_reasons': Counter(), 'http_attempts': 0,
                                         'reserved_weight': 0, 'reconciled_weight': 0})
        group['rounds'] += 1
        group['round_seconds'].append(row['seconds'])
        group['captures'] += row['captures']
        group['model_calls'] += row['model_calls']
        group['review_reasons'].update(row['review_reasons'])
        for event in events:
            if event['round'] != number or event['phase'] != row['phase']:
                continue
            if event['kind'] in ('capture_to_model', 'capture'):
                group[event['kind']].append(event['seconds'])
            if event['kind'] == 'http' and (event['scope'] in ('warmup', 'volume', 'fault-research', 'smoke')
                                           or event['scope'].startswith('vol_delta')):
                group['http_attempts'] += 1
                group['reserved_weight'] += event['reserved']
                group['reconciled_weight'] += event['actual'] if event['actual'] is not None else event['reserved']
    for group in groups.values():
        for metric in ('capture_to_model', 'capture', 'round_seconds'):
            group[metric] = distribution(group[metric])
        group['review_reasons'] = dict(group['review_reasons'])

    # Completion events carry transport durations. Reconstruct approximate start
    # times, separately per isolated coordinator; never sum unrelated quotas.
    scopes = {}
    instances = Counter()
    last_scope = None
    for event in events:
        scope = event.get('scope')
        if scope is not None:
            # Revalidation probes instantiate a fresh coordinator each cycle.
            # Their family label is reused; an intervening scope starts the next instance.
            if scope != last_scope and scope.startswith('revalidation-'):
                instances[scope] += 1
            last_scope = scope
        if event['kind'] == 'http':
            key = scope+f'#{instances[scope]}' if scope.startswith('revalidation-') else scope
            scopes.setdefault(key, []).append(event)
    windows = {}
    for scope, calls in scopes.items():
        timeline = sorted((e['t']-e['seconds'], e['reserved'],
                           e['actual'] if e['actual'] is not None else e['reserved']) for e in calls)
        window = deque()
        used_reserved = used_actual = peak_reserved = peak_actual = 0
        for t, reserved, actual in timeline:
            while window and window[0][0]+60 <= t:
                _, old_reserved, old_actual = window.popleft()
                used_reserved -= old_reserved
                used_actual -= old_actual
            window.append((t, reserved, actual))
            used_reserved += reserved
            used_actual += actual
            peak_reserved = max(peak_reserved, used_reserved)
            peak_actual = max(peak_actual, used_actual)
        windows[scope] = {'peak_60s_original_estimates': peak_reserved,
                          'peak_60s_final_response_weights': peak_actual}
    comparisons = {}
    batch = [g for k, g in groups.items() if k.startswith('volume:pool10:incrementalFalse')]
    incremental = [g for k, g in groups.items() if k.startswith('volume:pool10:incrementalTrue')]
    if batch and incremental:
        b, i = batch[0], incremental[0]
        bp, ip = b['capture_to_model']['p95'], i['capture_to_model']['p95']
        comparisons['volume'] = {'batch_p95_seconds': bp, 'incremental_p95_seconds': ip,
                                 'reduction_pct': 100*(bp-ip)/bp if bp and ip is not None else None,
                                 'batch_review_reasons': b['review_reasons'],
                                 'incremental_review_reasons': i['review_reasons']}
    context_events = [e for e in events if e['kind'] == 'context_use']
    unique_contexts = {(e['phase'], e['round'], e['timestamp_ms']) for e in context_events}
    model_starts = {(e['phase'], e['round'], e['symbol']): e['t']
                    for e in events if e['kind'] == 'capture_to_model'}
    observed_model_seconds = [e['t']-model_starts[(e['phase'], e['round'], e['symbol'])]
                              for e in events if e['kind'] == 'model'
                              and (e['phase'], e['round'], e['symbol']) in model_starts]
    corrected_reuse = len(context_events)-len(unique_contexts)
    corrections = []
    if summary['context_reused_uses'] != corrected_reuse:
        corrections.append({'metric': 'context_reused_uses', 'category': 'harness_metric',
                            'raw_summary_value': summary['context_reused_uses'],
                            'corrected_value': corrected_reuse,
                            'reason': 'Synthetic timestamp collision across independent rounds/processes.'})
    return {'mode': manifest['mode'], 'passed': summary['passed'],
            'elapsed_seconds': summary['elapsed_seconds'], 'violations': summary['violations'],
            'http_requests': summary['http_requests'],
            'reserved_attempt_weight': summary['reserved_attempt_weight'],
            'reconciled_attempt_weight': summary['reconciled_attempt_weight'],
            'coordinators': summary['coordinators'],
            'metric_corrections': corrections,
            'groups': groups, 'comparisons': comparisons, 'rolling_windows': windows,
            'context_uses': len(context_events), 'context_reused_uses': len(context_events)-len(unique_contexts),
            'context_units_avoided': 20*(len(context_events)-len(unique_contexts)),
            'observed_synthetic_model_duration': distribution(observed_model_seconds),
            'http_429_attempts': sum(e['kind'] == 'http' and e.get('status') == 429 for e in events),
            'fault_probes': [e for e in events if e['kind'] == 'fault_probe'],
            'revalidation': {scenario: distribution([e['seconds'] for e in events
                              if e['kind'] == 'revalidation' and e['scenario'] == scenario])
                             for scenario in ('normal', 'blocked', 'exhausted', '429')},
            'phases': [e for e in events if e['kind'] == 'phase_end'],
            'notes': ['Rolling transport windows are reconstructed from completion timestamps and durations.',
                      'Original estimates can exceed 900 cumulatively after reconciliation refunds.',
                      'Debt is isolated and expected to exceed 900; atomic admission checks are authoritative.',
                      'Invoked-only capture-to-model percentiles exclude expired batch snapshots.',
                      'context_units_avoided is relative to uncached successful provider uses.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    result = analyze(args.directory)
    (args.directory/'analysis.json').write_text(json.dumps(result, indent=2, sort_keys=True)+'\n')
    lines = ['# Offline REST benchmark', '',
             f"Passed: {result['passed']}; elapsed: {result['elapsed_seconds']:.3f}s.", '',
             f"HTTP attempts: {result['http_requests']}; gross reserved weight: {result['reserved_attempt_weight']}; "
             f"reconciled attempt weight: {result['reconciled_attempt_weight']}.", '',
             f"Context uses: {result['context_uses']}; reused uses: {result['context_reused_uses']}; "
             f"units avoided relative to uncached provider uses: {result['context_units_avoided']}.", '',
             '| Group | Rounds | Captures | Models | Capture p95 (s) | Capture→model p95 (s) |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for name, group in result['groups'].items():
        lines.append(f"| {name} | {group['rounds']} | {group['captures']} | {group['model_calls']} | "
                     f"{group['capture']['p95']} | {group['capture_to_model']['p95']} |")
    lines += ['', '## Comparison', '']
    if 'volume' in result['comparisons']:
        comparison = result['comparisons']['volume']
        lines += [f"Invoked-only capture→model p95: batch {comparison['batch_p95_seconds']:.6f}s; "
                  f"incremental {comparison['incremental_p95_seconds']:.6f}s "
                  f"({comparison['reduction_pct']:.1f}% reduction).", '',
                  'Batch review reasons: '+json.dumps(comparison['batch_review_reasons'])+'.', '',
                  'Incremental review reasons: '+json.dumps(comparison['incremental_review_reasons'])+'.', '']
    lines += ['## Fault probes', '', '| Fault | Code | Seconds |', '| --- | --- | ---: |']
    lines += [f"| {p['fault']} | {p['code']} | {p['seconds']:.6f} |" for p in result['fault_probes']]
    lines += ['', '## Revalidation', '', '| Scenario | Samples | p95 (s) | Max (s) |',
              '| --- | ---: | ---: | ---: |']
    lines += [f"| {name} | {row['n']} | {row['p95']} | {row['max']} |"
              for name, row in result['revalidation'].items()]
    lines += ['', '## Violations', '', json.dumps(result['violations']), '', '## Interpretation', '']
    lines += ['- '+note for note in result['notes']]
    if result['metric_corrections']:
        lines += ['', 'Derived metric corrections (original summary retained):', '',
                  json.dumps(result['metric_corrections']), '']
    lines += ['', '## Recommendations', '',
              '- Preserve incremental admission and the current freshness gates.',
              '- Treat expected faults and blocked expired batch snapshots separately from invariant violations.',
              '- Review total stage latency as well as dispatch latency: round order and retained REST budget affect waits.',
              '- Extend future testing with sustained concurrent 429s, admission fairness and cooperative process quotas.',
              '- These synthetic results do not authorize mainnet tests or prove an external per-IP quota.']
    (args.directory/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'passed': result['passed'], 'comparisons': result['comparisons'],
                      'groups': len(result['groups']), 'violations': len(result['violations'])}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
