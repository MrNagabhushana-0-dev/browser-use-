"""Measure a watcher sub-agent's run from its own transcript (Claude Code JSONL): METRIC-A and METRIC-B.

Definitions are fixed in docs/contract/LOOP_MEMORY.md; do not change them to move a number.

usage: python scripts/contract/measure_watch.py <agent transcript .jsonl> [--tool retinat_watch]
"""

import argparse
import json
import re
import statistics
from datetime import datetime


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


def measure(path: str, tool: str) -> dict:
	lines = [json.loads(line) for line in open(path)]
	usage: dict[str, tuple[float, dict]] = {}  # message id -> (first timestamp, usage): one message, many lines
	calls: dict[str, float] = {}  # tool_use id -> when the call was issued
	results: list[tuple[float, float, str]] = []  # (call issued, result received, result text)
	for line in lines:
		msg = line.get('message') or {}
		when = _ts(line['timestamp']) if line.get('timestamp') else None
		if line.get('type') == 'assistant' and msg.get('id') and msg.get('usage'):
			usage.setdefault(msg['id'], (when, msg['usage']))
		for block in msg.get('content') or [] if isinstance(msg.get('content'), list) else []:
			if block.get('type') == 'tool_use' and block.get('name', '').endswith(tool):
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
	latencies, delays, video = [], [], 0.0
	for issued, received, text in results:
		delays.append(received - issued)
		covering = re.search(rf'covering {_T}-{_T}', text)
		if not covering:
			continue
		v0, v1 = _seconds(covering[1]), _seconds(covering[2])
		video += v1 - v0
		# each timeline row after the first starts an event (a cut, a fade, motion starting or stopping)
		rows = [_seconds(m[1]) for m in re.finditer(rf'^\s+{_T}-{_T}\s', text, re.M)]
		# the model sees nothing of a window before it closes at v1: an event at v waited at least v1 - v (the time
		# the result took to build after the window closed comes on top, unseen here)
		for v in rows[1:]:
			latencies.append(v1 - v)
	return {
		'calls': len(results),
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
				'p90': round(sorted(latencies)[int(0.9 * (len(latencies) - 1))], 2),
			}
			if latencies
			else None
		),
	}


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('transcript')
	parser.add_argument('--tool', default='retinat_watch')
	args = parser.parse_args()
	print(json.dumps(measure(args.transcript, args.tool), indent=1))
