#!/usr/bin/env python3
"""task_complete: the completion gate of RETINAT_CONTRACT section 4.6.

A milestone run is not "complete" because an agent says so. This script checks four preconditions, prints which
are met, and only when ALL four are met appends exactly one line to docs/contract/LOOP_MEMORY.md:

	TASK_COMPLETE <milestone> <run_id> <git HEAD short> <UTC time> verifier=<file> recording=<file>

Otherwise it exits 1 and appends nothing.

Usage:
	python3 scripts/contract/task_complete.py MILESTONE RUN_ID \\
		--verifier FILE --recording PATH --measure FILE [--memory FILE] [--repo DIR]

Preconditions:
	verifier    --verifier FILE exists, is a regular file and is non-empty (the verifier agent's output).
	recording   --recording PATH exists (a file, or a directory of frames); a zero-byte file does not count.
	measure     --measure FILE exists and is a non-empty JSON document (it must parse).
	decision    the memory file has a line that states a final KEEP or REVERT (upper case) right next to RUN_ID, as
	            a whole token (E1 does not match E10 or E1-b): "RUN_ID DECISION: KEEP", "RUN_ID: REVERT" or "KEEP RUN_ID".
	            "KEEP, pending the verifier" and "KEEP if ..." do not count. The matching line is printed: read it.

Behaviour:
	--memory FILE  override the memory file (default docs/contract/LOOP_MEMORY.md of this repository); use a throwaway
	               copy to try the gate without touching the real log.
	--repo DIR     the repository whose HEAD is stamped (default: the repository this script lives in).
	Re-running for a (milestone, run_id) that already has a TASK_COMPLETE line appends nothing and exits 0.
	Exit codes: 0 complete (appended, or already recorded), 1 not complete / nothing appended, 2 bad usage.
	The memory file is append-only: it is opened for append under an exclusive lock, never rewritten.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_MEMORY = REPO / 'docs' / 'contract' / 'LOOP_MEMORY.md'
TOKEN_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:+-]*$')
GIT_TIMEOUT_S = 10


@dataclass
class Check:
	name: str
	ok: bool
	detail: str


def check_verifier(path: Path) -> Check:
	if not path.is_file():
		return Check('verifier output', False, f'{path} is not an existing file')
	size = path.stat().st_size
	if size == 0:
		return Check('verifier output', False, f'{path} is empty')
	return Check('verifier output', True, f'{path} ({size} bytes)')


def check_recording(path: Path) -> Check:
	if not path.exists():
		return Check('recording', False, f'{path} does not exist')
	if path.is_file() and path.stat().st_size == 0:
		return Check('recording', False, f'{path} is a zero-byte file')
	kind = f'{path.stat().st_size} bytes' if path.is_file() else 'directory'
	return Check('recording', True, f'{path} ({kind})')


def check_measure(path: Path) -> Check:
	if not path.is_file():
		return Check('measurement JSON', False, f'{path} is not an existing file')
	try:
		value = json.loads(path.read_text(encoding='utf-8'))
	except (OSError, ValueError) as exc:
		return Check('measurement JSON', False, f'{path} is not readable JSON ({type(exc).__name__}: {str(exc)[:80]})')
	if not isinstance(value, (dict, list)) or not value:
		return Check('measurement JSON', False, f'{path} holds no measurement (an empty or non-object JSON value)')
	return Check('measurement JSON', True, f'{path} ({path.stat().st_size} bytes, parses to a non-empty object)')


def decision_line(memory_text: str, run_id: str) -> str | None:
	"""First line that states a final KEEP or REVERT for the run id, else None.

	The id must be a whole token and the verdict must sit next to it: "E2 DECISION: KEEP", "E2: REVERT", "KEEP E2".
	A verdict that is still pending ("KEEP, pending the verifier") or conditional ("KEEP if ...") does not count.
	"""
	named = rf'(?<![A-Za-z0-9_-])(?:{re.escape(run_id)})(?![A-Za-z0-9_]|[-.][A-Za-z0-9])'
	verdict = r'(?:KEEP|REVERT)\b(?!\W*pending\b)(?!\s+(?:if|when|unless)\b)'
	asserted = re.compile(rf'{named}\W{{0,3}}(?:DECISION\W{{0,3}})?{verdict}|\b{verdict}\W{{0,3}}{named}')
	for line in memory_text.splitlines():
		if line.startswith('TASK_COMPLETE '):
			continue
		if asserted.search(line):
			return line.strip()
	return None


def check_decision(memory: Path, run_id: str) -> Check:
	try:
		text = memory.read_text(encoding='utf-8', errors='replace')
	except OSError as exc:
		return Check('KEEP/REVERT line', False, f'cannot read {memory}: {exc.strerror or exc}')
	line = decision_line(text, run_id)
	if line is None:
		return Check('KEEP/REVERT line', False, f'no line in {memory.name} states a final KEEP or REVERT for {run_id}')
	shown = line if len(line) <= 110 else line[:107] + '...'
	return Check('KEEP/REVERT line', True, f'{memory.name}: "{shown}"')


def git_head_short(repo: Path) -> str | None:
	try:
		proc = subprocess.run(
			['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=GIT_TIMEOUT_S
		)
	except (OSError, subprocess.SubprocessError):
		return None
	head = proc.stdout.strip()
	return head if proc.returncode == 0 and re.fullmatch(r'[0-9a-f]{4,40}', head) else None


def completion_line(milestone: str, run_id: str, head: str, verifier: str, recording: str) -> str:
	stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
	return (
		f'TASK_COMPLETE {milestone} {run_id} {head} {stamp} verifier={shlex.quote(verifier)} recording={shlex.quote(recording)}'
	)


def append_once(memory: Path, milestone: str, run_id: str, line: str) -> str | None:
	"""Append `line` under an exclusive lock. Returns the existing TASK_COMPLETE line if one is already recorded."""
	prefix = f'TASK_COMPLETE {milestone} {run_id} '
	with open(memory, 'a+', encoding='utf-8') as fh:
		fcntl.flock(fh, fcntl.LOCK_EX)
		fh.seek(0)
		content = fh.read()
		for existing in content.splitlines():
			if existing.startswith(prefix):
				return existing
		fh.seek(0, 2)
		fh.write(('' if not content or content.endswith('\n') else '\n') + line + '\n')
	return None


def _log_report(milestone: str, run_id: str, checks: list[Check]) -> None:
	print(f'task_complete: milestone={milestone} run_id={run_id}')
	for check in checks:
		print(f'  [{"ok" if check.ok else "MISSING"}]'.ljust(11) + f' {check.name}: {check.detail}')


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description='Completion gate: append TASK_COMPLETE only when every precondition is met.')
	parser.add_argument('milestone')
	parser.add_argument('run_id')
	parser.add_argument('--verifier', required=True, metavar='FILE')
	parser.add_argument('--recording', required=True, metavar='PATH')
	parser.add_argument('--measure', required=True, metavar='FILE')
	parser.add_argument('--memory', type=Path, default=DEFAULT_MEMORY, metavar='FILE')
	parser.add_argument('--repo', type=Path, default=REPO, metavar='DIR')
	args = parser.parse_args(argv)
	for label, value in (('milestone', args.milestone), ('run_id', args.run_id)):
		if not TOKEN_RE.match(value):
			parser.error(f'{label} {value!r} must be one token of letters, digits and . _ : + - (it goes into a log line)')

	checks = [
		check_verifier(Path(args.verifier)),
		check_recording(Path(args.recording)),
		check_measure(Path(args.measure)),
		check_decision(args.memory, args.run_id),
	]
	_log_report(args.milestone, args.run_id, checks)
	met = sum(c.ok for c in checks)
	if met != len(checks):
		print(f'NOT COMPLETE: {met} of {len(checks)} preconditions met; nothing appended to {args.memory}')
		return 1
	head = git_head_short(args.repo)
	if head is None:
		print(f'NOT COMPLETE: all preconditions met but git HEAD of {args.repo} is unreadable; nothing appended to {args.memory}')
		return 1
	line = completion_line(args.milestone, args.run_id, head, args.verifier, args.recording)
	try:
		existing = append_once(args.memory, args.milestone, args.run_id, line)
	except OSError as exc:
		print(
			f'NOT COMPLETE: all preconditions met but {args.memory} is not appendable ({exc.strerror or exc}); nothing appended'
		)
		return 1
	if existing is not None:
		print(f'ALREADY RECORDED in {args.memory}: {existing}')
		return 0
	print(f'COMPLETE: appended to {args.memory}:\n{line}')
	return 0


if __name__ == '__main__':
	sys.exit(main())
