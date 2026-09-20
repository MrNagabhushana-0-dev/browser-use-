"""Models for tools synthesized from a page's own affordances."""

import json
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

MAX_TOOLS_PER_SITE = 24
MAX_STEPS_PER_TOOL = 12

_NON_IDENT = re.compile(r'[^a-z0-9]+')
_STOPWORDS = {'the', 'a', 'an', 'your', 'my', 'please', 'click', 'button', 'field', 'input'}


def to_identifier(text: str, fallback: str = 'action') -> str:
	"""Turn a human label into a tool or parameter name.

	"Add to cart" becomes add_to_cart. The name is what a model will type back, so it has
	to be predictable from the label a person sees on the page.
	"""
	words = [w for w in _NON_IDENT.sub(' ', text.lower()).split() if w and w not in _STOPWORDS]
	name = '_'.join(words[:4]) or fallback
	if name[0].isdigit():
		name = f'{fallback}_{name}'
	return name[:48]


def _clip(value: str) -> str:
	return value if len(value) <= 300 else value[:299] + '…'


class Locator(BaseModel):
	"""How to find a control again after the page re-renders.

	Ordered by how long each handle survives in practice: a test id is the one attribute
	authors keep stable deliberately, an id is usually stable, role plus accessible name is
	what a person would say out loud and survives restyling, and a CSS path is the last
	resort that breaks first.
	"""

	model_config = ConfigDict(extra='ignore')

	testid: str | None = None
	id: str | None = None
	role: str = ''
	name: str = ''
	css: str | None = None

	def describe(self) -> str:
		if self.testid:
			return f'[data-testid={self.testid}]'
		if self.id:
			return f'#{self.id}'
		if self.name:
			return f'{self.role or "element"} "{self.name}"'
		return self.css or 'unknown element'


class ToolStep(BaseModel):
	"""One UI operation in a synthesized tool."""

	model_config = ConfigDict(extra='forbid')

	action: Literal['fill', 'click', 'select', 'press', 'read', 'set_checked']
	locator: Locator
	# Which tool parameter supplies this step's value; None for a plain click.
	param: str | None = None
	# For 'press': the key to send, e.g. Enter. A search box outside a form has no submit
	# button to click, and Enter is how a person submits it.
	key: str | None = None
	# For 'read': the column headers seen at synthesis time, so the tool can say what it
	# returns before anyone runs it.
	columns: list[str] = Field(default_factory=list)


class SynthesizedTool(BaseModel):
	"""A typed tool induced from what the page lets a person do."""

	model_config = ConfigDict(extra='forbid')

	name: str
	description: Annotated[str, AfterValidator(_clip)] = ''
	input_schema: dict[str, Any] = Field(default_factory=dict)
	steps: list[ToolStep] = Field(default_factory=list)
	# Set once the tool has been run and observed to change the page. Until then it is a
	# guess about what the markup means, and is labelled as one.
	verified: bool = False

	def signature(self) -> str:
		properties = self.input_schema.get('properties') or {}
		required = set(self.input_schema.get('required') or [])
		params = [f'{key}{"" if key in required else "?"}: {spec.get("type", "string")}' for key, spec in properties.items()]
		return f'{self.name}({", ".join(params)})'


class SiteManifest(BaseModel):
	"""The tool surface induced for one origin."""

	model_config = ConfigDict(extra='forbid')

	origin: str
	url: str = ''
	title: str = ''
	tools: list[SynthesizedTool] = Field(default_factory=list)
	# Hash of the affordances this was built from. A cached surface for a page that has
	# since been redesigned is worse than no cache: it fails in a way that reads as the
	# agent being wrong rather than the cache being stale.
	fingerprint: str = ''
	created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

	def get(self, name: str) -> SynthesizedTool | None:
		for tool in self.tools:
			if tool.name == name:
				return tool
		return None

	def to_json(self) -> str:
		return json.dumps(self.model_dump(mode='json'), indent=1)
