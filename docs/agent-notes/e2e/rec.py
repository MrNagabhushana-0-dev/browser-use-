import json
import sys
from pathlib import Path

p = Path(__file__).parent / 'results.json'
r = json.loads(p.read_text())
rid, answer, tokens, ms, calls = sys.argv[1:6]
r[rid] = {'answer': answer, 'tokens': int(tokens), 'seconds': int(ms) / 1000, 'calls': calls}
p.write_text(json.dumps(r, indent=1))
print(len(r), 'recorded')
