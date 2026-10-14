"""A Claude Code hook that tells the model what the browser is showing, every turn, unasked.

A tool the model has to remember to call is not an eye. An eye is there whether you think
of it or not. Claude Code's hooks can add text to the model's context on each prompt and
after each tool call, so this prints the retina's current one-line reading (written by
`Eyes` to `now.json` about once a second) as that context:

    👁 watching a video "@user caption…" · at 4.2s of 12.0s · dark blue, moving · sound: speech (moderate)

Register it in `.claude/settings.json` (hooks run `python -m browser_use.eyes.hook`):

    {"hooks": {
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python -m browser_use.eyes.hook"}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python -m browser_use.eyes.hook"}]}]
    }}

Hooks can only add text, not images; the pictures still come from `eyes_watch` (and frames from a
moment already seen, from `retinat_recall`). A reading older than `STALE_S` is not reported: a stale
eye is worse than none.

The eyes also keep a journal of what *changed* (`journal.jsonl` next to `now.json`): a new item, a
sound change, a pause. Each turn the hook adds the entries it has not shown yet, newest few only, so
what happened between turns reaches the model without the eyes' raw stream filling its context.
"""

import json
import sys
import time
from pathlib import Path

STALE_S = 30.0
MAX_ENTRIES = 8


def reading(path: Path | None = None, now: float | None = None) -> str | None:
	"""The current one-line reading, or None if there is none or it is stale."""
	if path is None:
		from browser_use.eyes.service import default_now_path

		path = default_now_path()
	try:
		data = json.loads(path.read_text())
	except Exception:
		return None
	age = (now or time.time()) - float(data.get('updated', 0))
	if age > STALE_S or not data.get('line'):
		return None
	return f'{data["line"]} ({age:.0f}s ago, browser-use eyes)'


def new_entries(path: Path | None = None, limit: int = MAX_ENTRIES, reader: str = 'hook') -> list[str]:
	"""Journal entries `reader` has not been shown (marks them shown), newest `limit`, as lines.

	Each reader keeps its own place, so the hook and an MCP tool do not take entries from each other.
	"""
	if path is None:
		from browser_use.eyes.service import default_now_path

		path = default_now_path()
	journal = path.with_name('journal.jsonl')
	offset = path.with_name('journal.offset' if reader == 'hook' else f'journal.{reader}.offset')
	try:
		lines = journal.read_text().splitlines()
	except Exception:
		return []
	try:
		seen = int(offset.read_text().strip() or 0)
	except Exception:
		seen = 0
	if seen > len(lines):  # the journal was trimmed: start from what is there
		seen = 0
	fresh = lines[seen:]
	try:
		offset.write_text(str(len(lines)))
	except Exception:
		pass
	entries = []
	for raw in fresh:
		try:
			entries.append(json.loads(raw))
		except Exception:
			continue
	entries = _fold_labels(entries)
	kept = _keep(entries, limit)
	out = []
	for e in kept:
		when = time.strftime('%H:%M:%S', time.localtime(float(e.get('at', 0))))
		where = f' (item {e["vid"]} at {e["t"]:.1f}s)' if e.get('vid') and isinstance(e.get('t'), (int, float)) else ''
		out.append(f'  {when} {e.get("text", "")}{where}')
	left = [e for e in entries if not any(e is k for k in kept)]
	if left:
		kinds: dict[str, int] = {}
		for e in left:
			kind = str(e.get('kind') or 'change')
			kinds[kind] = kinds.get(kind, 0) + 1
		said = ', '.join(f'{n} earlier {kind}(s)' for kind, n in kinds.items())
		out.insert(0, f'  ({said} not shown)')
	return out


# A label this short, in a burst within one second, is part of something redrawn (a grid's column letters, a row of
# numbers), not news one by one (found on Puter's spreadsheet: "A", "B", "C" ... one line each crowded out the cells)
SHORT_LABEL = 3


def _label(e: dict) -> str | None:
	"""The text of a text-appeared entry, if it is a short label."""
	text = str(e.get('text', ''))
	if e.get('kind') != 'text' or not text.startswith('text appeared: "'):
		return None
	body = text[len('text appeared: "') :].split('"', 1)[0]
	return body if 0 < len(body) <= SHORT_LABEL else None


def _fold_labels(entries: list[dict]) -> list[dict]:
	"""Runs of five or more short labels within one second become one entry."""
	out: list[dict] = []
	run: list[dict] = []

	def flush() -> None:
		if len(run) >= 5:
			labels = [_label(e) for e in run]
			shown = ', '.join(f'"{t}"' for t in labels[:3]) + f', … "{labels[-1]}"'
			out.append({**run[-1], 'text': f'text appeared: {len(run)} short labels ({shown})'})
		else:
			out.extend(run)
		run.clear()

	for e in entries:
		if _label(e) is not None and (not run or int(float(e.get('at', 0))) == int(float(run[0].get('at', 0)))):
			run.append(e)
			continue
		flush()
		if _label(e) is not None:
			run.append(e)
		else:
			out.append(e)
	flush()
	return out


def _keep(entries: list[dict], limit: int) -> list[dict]:
	"""At most `limit` entries, in order: pages, items, sounds and requests before texts, then the newest texts."""
	if len(entries) <= limit:
		return entries
	others = [e for e in entries if e.get('kind') != 'text'][-limit:]
	room = limit - len(others)
	texts = [e for e in entries if e.get('kind') == 'text'][-room:] if room > 0 else []
	chosen = {id(e) for e in others + texts}
	return [e for e in entries if id(e) in chosen]


def main() -> int:
	try:
		event = json.loads(sys.stdin.read() or '{}')
	except Exception:
		event = {}
	line = reading()
	changes = new_entries()
	if line is None and not changes:
		return 0
	parts = []
	if changes:
		parts.append('Since your last turn (browser-use eyes; frames via retinat_recall):\n' + '\n'.join(changes))
	if line is not None:
		parts.append(f'What the browser shows right now: {line}')
	name = event.get('hook_event_name') or 'UserPromptSubmit'
	print(json.dumps({'hookSpecificOutput': {'hookEventName': name, 'additionalContext': '\n'.join(parts)}}))
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
