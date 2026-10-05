"""Score blind agent answers against truth. Rules fixed before scoring the remaining runs:
colour: correct if the answer names the same colour (lime == green, the #00ff00 primary; magenta == fuchsia/pink-purple;
cyan == aqua; yellow); count: the exact integer; toast id: the exact 5 digits. UNKNOWN or anything else is wrong."""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
SYNONYMS = {'lime': {'lime', 'green'}, 'magenta': {'magenta', 'fuchsia'}, 'cyan': {'cyan', 'aqua'}, 'yellow': {'yellow'}}


def correct(run: dict, answer: str) -> bool:
	a = answer.strip().lower()
	if run['task'] == 'flash':
		words = set(re.findall(r'[a-z]+', a))
		return bool(words & SYNONYMS[run['truth']]) and not any(words & s for c, s in SYNONYMS.items() if c != run['truth'])
	nums = re.findall(r'\d+', a)
	return len(nums) == 1 and int(nums[0]) == int(run['truth'])


if __name__ == '__main__':
	truth = {r['id']: r for r in json.loads((HERE / 'truth.json').read_text())}
	results = json.loads((HERE / 'results.json').read_text())
	rows = []
	for rid, res in results.items():
		if ':' in rid:
			print(f'{rid.split(":", 1)[0]} {rid.split(":", 1)[1]}: {res["answer"]!r} vs {truth[rid.split(":", 1)[1]]["truth"]!r}', correct(truth[rid.split(":", 1)[1]], res['answer']))
			continue
		run = truth[rid]
		rows.append((run['task'], run['condition'], correct(run, res['answer']), res['tokens'], res['seconds'], res['answer'], run['truth']))
	for cond in ('retina', 'screenshots', 'dom', 'retina-late', 'screenshots-late'):
		for task in ('flash', 'beeps', 'toast', 'bounce'):
			rs = [r for r in rows if r[0] == task and r[1] == cond]
			if rs:
				print(f'{task:6} {cond:12} {sum(r[2] for r in rs)}/{len(rs)}  tokens~{sum(r[3] for r in rs) // len(rs):>6}  s~{sum(r[4] for r in rs) / len(rs):5.0f}  '
					+ '; '.join(f'{r[5]!r} vs {r[6]!r}' for r in rs))
