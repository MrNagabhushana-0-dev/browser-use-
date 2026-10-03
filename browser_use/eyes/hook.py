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


def new_entries(path: Path | None = None, limit: int = MAX_ENTRIES) -> list[str]:
	"""Journal entries not reported before (marks them reported), newest `limit`, as lines."""
	if path is None:
		from browser_use.eyes.service import default_now_path

		path = default_now_path()
	journal, offset = path.with_name('journal.jsonl'), path.with_name('journal.offset')
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
	out = []
	for raw in fresh[-limit:]:
		try:
			e = json.loads(raw)
		except Exception:
			continue
		when = time.strftime('%H:%M:%S', time.localtime(float(e.get('at', 0))))
		where = f' (item {e["vid"]} at {e["t"]:.1f}s)' if e.get('vid') and isinstance(e.get('t'), (int, float)) else ''
		out.append(f'  {when} {e.get("text", "")}{where}')
	if len(fresh) > limit:
		out.insert(0, f'  ({len(fresh) - limit} earlier change(s) not shown)')
	return out


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
