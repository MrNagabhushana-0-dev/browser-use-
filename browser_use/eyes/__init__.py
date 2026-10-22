"""The browser as the agent's eyes and ears.

The retina (`retina.js`, in an isolated world) taps the video a person would be looking at:
its decoded frames and its audio, measured in the page and pushed out over a CDP binding.
`sight` and `hearing` read those measurements; `percept` turns them into one sheet and a few
lines of text; `Eyes` watches, decides when it has seen enough, and moves on with a thumb.

Needs numpy (`pip install "browser-use[eyes]"`, which also brings the optional speech model).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.eyes.percept import ItemPercept, Percept, estimate_image_tokens
	from browser_use.eyes.retina import AudioHop, FrameSample, Retina
	from browser_use.eyes.service import Eyes, NextResult

__all__ = ['AudioHop', 'Eyes', 'FrameSample', 'ItemPercept', 'NextResult', 'Percept', 'Retina', 'estimate_image_tokens']

# Imported on first use: the numpy-free parts (`views`, the call budget an MCP server's front reads) load without numpy
_HOME = {
	'ItemPercept': 'percept',
	'Percept': 'percept',
	'estimate_image_tokens': 'percept',
	'AudioHop': 'retina',
	'FrameSample': 'retina',
	'Retina': 'retina',
	'Eyes': 'service',
	'NextResult': 'service',
}


def __getattr__(name: str):
	if name in _HOME:
		import importlib

		return getattr(importlib.import_module(f'browser_use.eyes.{_HOME[name]}'), name)
	raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
