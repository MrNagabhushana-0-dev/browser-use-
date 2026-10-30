"""Measure live narration from a run's narration log (eyes/narration.py): was each result narrated before the next
watch, how long after the result, and event-on-screen -> narration latency for the film's own cuts.

B_narr for a cut at film second v that a result covering a-b reported: (b - v) + lag + (narrated - returned), where lag
is how long before the answer the result's last sample was taken (logged by the server) and the narration's own wall
time minus the result's return time is the model's turn. It assumes the film played at 1x between v and b.

usage: python scripts/contract/measure_narration.py <narration.jsonl> [--edits edits.json] [--transcript agent.jsonl]
"""

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from measure_watch import _T, _seconds  # noqa: E402  (the counter's own time parser, unchanged)


def p90(xs: list[float]) -> float:
	xs = sorted(xs)
	k = 0.9 * (len(xs) - 1)
	return xs[int(k)] + (xs[min(int(k) + 1, len(xs) - 1)] - xs[int(k)]) * (k - int(k))


def measure(path: str, edits: str | None = None, transcript: str | None = None) -> dict:
	entries = [json.loads(line) for line in open(path) if line.strip()]
	results = {e['n']: e for e in entries if e['kind'] == 'result'}
	narrations = {e['n']: e for e in entries if e['kind'] == 'narration' and e['n']}
	assert results, f'no live watch results in {path}'
	delays = [narrations[n]['wall'] - results[n]['wall'] for n in results if n in narrations]
	cuts = json.load(open(edits))['cuts'] if edits else []
	latencies = []
	for n, r in results.items():
		span = re.match(rf'{_T}-{_T}$', r['covering'] or '')
		if not span or n not in narrations or r.get('lag') is None:
			continue
		a, b = _seconds(span[1]), _seconds(span[2])
		for v in cuts:
			if a <= v <= b:
				latencies.append((b - v) + r['lag'] + (narrations[n]['wall'] - r['wall']))
	out: dict = {
		'results': len(results),
		'narrated': sum(1 for n in results if n in narrations),
		'unnarrated': sorted(n for n in results if n not in narrations),
		'narration_after_result_s': (
			{'median': round(statistics.median(delays), 2), 'max': round(max(delays), 2)} if delays else None
		),
		'B_narr_s': (
			{'n': len(latencies), 'median': round(statistics.median(latencies), 2), 'p90': round(p90(latencies), 2)}
			if latencies
			else None
		),
	}
	if transcript:
		# the transcript's own order: every watch call after the first carries a narration, and none is batched at the end
		calls = []
		for line in open(transcript):
			msg = json.loads(line).get('message') or {}
			for block in msg.get('content') or [] if isinstance(msg.get('content'), list) else []:
				if block.get('type') == 'tool_use' and block.get('name', '').endswith(('eye_watch', 'eye_narrate', 'retinat_watch', 'retinat_narrate')):
					calls.append(
						(
							block['name'].rsplit('_', 1)[-1],
							bool(
								str(
									(block.get('input') or {}).get('narration') or (block.get('input') or {}).get('text') or ''
								).strip()
							),
						)
					)
		watches = [c for c in calls if c[0] == 'watch']
		out['transcript'] = {
			'watch_calls': len(watches),
			'watch_calls_after_first_with_narration': sum(1 for _, has in watches[1:] if has),
			'narrate_calls': sum(1 for c in calls if c[0] == 'narrate'),
		}
	return out


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('log')
	parser.add_argument('--edits')
	parser.add_argument('--transcript')
	args = parser.parse_args()
	print(json.dumps(measure(args.log, args.edits, args.transcript), indent=1))
