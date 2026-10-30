"""Measure a watcher sub-agent's run from its own transcript (Claude Code JSONL): METRIC-A and METRIC-B.

Definitions are fixed in docs/contract/LOOP_MEMORY.md; do not change them to move a number.

usage: python scripts/contract/measure_watch.py <agent transcript .jsonl> [--tool eye_watch]
"""

import argparse
import json
import re
import statistics
from datetime import datetime

# tool names: the watch tool was retinat_watch until the 2026-10-30 rename to eye_watch; both are read, so every
# earlier transcript measures the same (correction logged in LOOP_MEMORY, 2026-10-30)
_ALIASES = {'eye_watch': ('eye_watch', 'retinat_watch')}
CUT_MATCH_S = 0.7  # the eyes report a cut up to ~0.5 s late (6 samples a second), measured by both E0/E1 verifiers


def _ts(s: str) -> float:
	return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


# a time as the watch writes it: 12.3s, or 1:02.3 past a minute
_T = r'(\d+:\d+(?:\.\d+)?|\d+(?:\.\d+)?s)'


def _seconds(t: str) -> float:
	if ':' in t:
		minutes, seconds = t.split(':')
		return int(minutes) * 60 + float(seconds)
	return float(t.rstrip('s'))


def _text(content) -> str:
	if isinstance(content, str):
		return content
	if isinstance(content, list):
		return '\n'.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
	return ''


def measure(path: str, tool: str, edits: str | None = None) -> dict:
	lines = [json.loads(line) for line in open(path)]
	usage: dict[str, tuple[float, dict]] = {}  # message id -> (first timestamp, usage): one message, many lines
	done: dict[str, float] = {}  # message id -> when it was finished (the first line after its last one)
	stamps = [_ts(line['timestamp']) for line in lines if line.get('timestamp')]
	last_id = None
	calls: dict[str, float] = {}  # tool_use id -> when the call was issued
	results: list[tuple[float, float, str]] = []  # (call issued, result received, result text)
	for line in lines:
		msg = line.get('message') or {}
		when = _ts(line['timestamp']) if line.get('timestamp') else None
		if last_id and when is not None and msg.get('id') != last_id and last_id not in done:
			done[last_id] = when
		# correction 4: a synthetic message (an API error the harness wrote, zero usage) is not a turn of the watcher
		real = msg.get('model') != '<synthetic>' and any(
			(msg.get('usage') or {}).get(k) for k in ('input_tokens', 'output_tokens')
		)
		if line.get('type') == 'assistant' and msg.get('id') and real:
			usage.setdefault(msg['id'], (when, msg['usage']))
			last_id = msg['id']
		for block in msg.get('content') or [] if isinstance(msg.get('content'), list) else []:
			if block.get('type') == 'tool_use' and block.get('name', '').endswith(_ALIASES.get(tool, (tool,))):
				calls[block['id']] = when
			elif block.get('type') == 'tool_result' and block.get('tool_use_id') in calls:
				results.append((calls[block['tool_use_id']], when, _text(block.get('content'))))
	assert results, f'no {tool} results in {path}'
	results.sort()
	start, end = results[0][0], results[-1][1]
	# the turns that took in what was watched: from the first watch call through the turn after the last result
	turns = sorted(usage.values(), key=lambda u: u[0])
	inside = [u for t, u in turns if start <= t <= end]
	after = [u for t, u in turns if t > end][:1]
	window = inside + after
	added = sum(u.get('input_tokens', 0) + u.get('cache_creation_input_tokens', 0) + u.get('output_tokens', 0) for u in window)
	processed = added + sum(u.get('cache_read_input_tokens', 0) for u in window)
	minutes = (end - start) / 60
	# A_unique (correction 2): every token once. A turn's output comes back as the next turn's cache_creation, so only the
	# last turn's output is added; the window runs from the first watch call to the watcher's last turn (where the
	# narration may be written), so all narration is counted
	later = [(t, u) for t, u in turns if t > start]
	last_t, last_u = later[-1]
	# correction 4: the span ends when the last turn was finished, not when it began (it holds the narration)
	last_id = next(i for i, (t, u) in usage.items() if u is last_u)
	last_t = done.get(last_id, stamps[-1] if stamps else last_t)
	unique = sum(u.get('input_tokens', 0) + u.get('cache_creation_input_tokens', 0) for _, u in later) + last_u.get(
		'output_tokens', 0
	)
	unique_minutes = (last_t - start) / 60
	latencies, delays, video, spans, reported = [], [], 0.0, [], []
	for issued, received, text in results:
		delays.append(received - issued)
		covering = re.search(rf'covering {_T}-{_T}', text)
		if not covering:
			continue
		v0, v1 = _seconds(covering[1]), _seconds(covering[2])
		spans.append((v0, v1))
		# each timeline row after the first starts an event (a cut, a fade, motion starting or stopping)
		rows = [_seconds(m[1]) for m in re.finditer(rf'^\s+{_T}-{_T}\s', text, re.M)]
		reported += rows
		# the model sees nothing of a window before it closes at v1: an event at v waited at least v1 - v (the time
		# the result took to build after the window closed comes on top, unseen here)
		for v in rows[1:]:
			latencies.append(v1 - v)
		# correction 3: a watch stopped by an event says so ("stopped: cut at 1:02.3"): the model is told of that event
		# in this result, though its frames open the next window (whose first row the loop above skips)
		stop = re.search(rf'stopped: \w+ at {_T}', text)
		# correction 4: a stop that is also a row of this result's timeline is one event, counted once
		if stop and not any(abs(_seconds(stop[1]) - r) <= 0.1 for r in rows[1:]):
			latencies.append(max(0.0, v1 - _seconds(stop[1])))
			reported.append(_seconds(stop[1]))
	# correction 4: film covered is the union of the windows (consecutive windows overlap by a sample since E2)
	reach_to = float('-inf')
	for v0, v1 in sorted(spans):
		video += max(0.0, v1 - max(v0, reach_to))
		reach_to = max(reach_to, v1)
	# film time no window covered between consecutive watches: what played there reached no percept
	gaps = [round(b0 - a1, 2) for (_, a1), (b0, _) in zip(spans, spans[1:]) if b0 > a1]

	def p90(xs: list[float]) -> float:  # linear interpolation (correction 2: the index rule read low)
		xs = sorted(xs)
		k = 0.9 * (len(xs) - 1)
		return xs[int(k)] + (xs[min(int(k) + 1, len(xs) - 1)] - xs[int(k)]) * (k - int(k))

	# cuts_reached_model (defined before protocol v2's first result): of the film's own cuts (its edit list) inside the
	# film span the windows covered, the share some result reported (a timeline row or a stop) within CUT_MATCH_S
	reach = None
	if edits and spans:
		cuts = [c for c in json.load(open(edits))['cuts'] if spans[0][0] <= c <= spans[-1][1]]
		found = [c for c in cuts if any(abs(r - c) <= CUT_MATCH_S for r in reported)]
		reach = {'cuts': len(cuts), 'reported': len(found), 'missed': [c for c in cuts if c not in found]}
	return {
		'calls': len(results),
		'cuts_reached_model': reach,
		'A_unique_per_min': round(unique / unique_minutes),
		'gaps_between_windows_s': {'n': len(gaps), 'total': round(sum(gaps), 2), 'max': max(gaps) if gaps else 0},
		'window_s': round(end - start, 1),
		'video_s_covered': round(video, 1),
		'A_added_per_min': round(added / minutes),
		'A_processed_per_min': round(processed / minutes),
		'A_added_per_video_min': round(added / (video / 60)) if video else None,
		'B_result_delay_s': {'median': round(statistics.median(delays), 2), 'max': round(max(delays), 2)},
		'B_event_latency_lower_bound_s': (
			{
				'n': len(latencies),
				'median': round(statistics.median(latencies), 2),
				'p90': round(p90(latencies), 2),
			}
			if latencies
			else None
		),
	}


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('transcript')
	parser.add_argument('--tool', default='eye_watch')
	parser.add_argument('--edits', help='the film edit list (JSON with "cuts" in film seconds)')
	args = parser.parse_args()
	print(json.dumps(measure(args.transcript, args.tool, args.edits), indent=1))
