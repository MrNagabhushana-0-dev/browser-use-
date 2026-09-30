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

Hooks can only add text, not images; the pictures still come from `eyes_watch`. A reading
older than `STALE_S` is not reported: a stale eye is worse than none.
"""

import json
import sys
import time
from pathlib import Path

STALE_S = 30.0


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


def main() -> int:
	try:
		event = json.loads(sys.stdin.read() or '{}')
	except Exception:
		event = {}
	line = reading()
	if line is None:
		return 0
	name = event.get('hook_event_name') or 'UserPromptSubmit'
	print(
		json.dumps(
			{'hookSpecificOutput': {'hookEventName': name, 'additionalContext': f'What the browser shows right now: {line}'}}
		)
	)
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
