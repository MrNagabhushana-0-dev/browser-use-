"""Install automatic agent memory for this checkout: venv (mem0ai + fastembed), hooks, and the daemon. Free, local.

usage: python3 scripts/memory/install.py [--home ~/agent-memory] [--no-start]

  1. uv venv <home> and install mem0ai + fastembed into it (no API key: embeddings run on this CPU).
  2. Merge three hooks into <repo>/.claude/settings.local.json (git-ignored, so memory never reaches the public repo):
     SessionStart (start the daemon), UserPromptSubmit (recall), Stop (remember). Existing settings are kept.
  3. Start the daemon now.
Kill switch: EYES_MEMORY=off. Remove: delete the hooks from settings.local.json; delete <home> to forget everything.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = 'python3 "${CLAUDE_PROJECT_DIR}/scripts/memory/hook.py"'
EVENTS = {'SessionStart': 15, 'UserPromptSubmit': 5, 'Stop': 15}


def ensure_venv(home: Path) -> None:
	if not (home / 'bin' / 'python').exists():
		subprocess.run(['uv', 'venv', str(home), '--python', '3.12', '-q'], check=True)
	subprocess.run(['uv', 'pip', 'install', '--python', str(home / 'bin' / 'python'), 'mem0ai', 'fastembed'], check=True)


def merge_hooks(settings_path: Path) -> list[str]:
	settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
	added = []
	hooks = settings.setdefault('hooks', {})
	for event, timeout in EVENTS.items():
		groups = hooks.setdefault(event, [])
		if any(h.get('command') == HOOK for g in groups for h in g.get('hooks', [])):
			continue  # already installed
		groups.append({'hooks': [{'type': 'command', 'command': HOOK, 'timeout': timeout}]})
		added.append(event)
	settings_path.parent.mkdir(parents=True, exist_ok=True)
	settings_path.write_text(json.dumps(settings, indent=2) + '\n')
	return added


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--home', type=Path, default=Path.home() / 'agent-memory')
	parser.add_argument('--no-start', action='store_true')
	args = parser.parse_args()
	ensure_venv(args.home)
	added = merge_hooks(REPO / '.claude' / 'settings.local.json')
	print(f'hooks added: {added or "none (already installed)"} in {REPO / ".claude" / "settings.local.json"}')
	if not args.no_start:
		hook_input = json.dumps({'hook_event_name': 'SessionStart'})
		subprocess.run([sys.executable, str(REPO / 'scripts' / 'memory' / 'hook.py')], input=hook_input, text=True, timeout=30)
		print('daemon start requested; check: curl with the bearer token in', args.home / 'token')


if __name__ == '__main__':
	main()
