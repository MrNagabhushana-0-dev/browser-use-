"""Induce reusable workflows from successful runs, and recall them on the next task."""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import ValidationError

from browser_use.memory.views import (
	MAX_STEPS_PER_WORKFLOW,
	UNINFORMATIVE_ACTIONS,
	VALUE_BEARING_ACTIONS,
	Workflow,
	WorkflowStep,
	render_workflow_memory,
)

if TYPE_CHECKING:
	from browser_use.agent.views import AgentHistoryList

logger = logging.getLogger(__name__)

# Per-domain cap. Memory that grows without bound stops being memory and becomes a log:
# retrieval gets noisier and the prompt block drifts toward irrelevant routes.
MAX_WORKFLOWS_PER_DOMAIN = 10

# Attributes that tend to carry a human-readable label, best first.
_LABEL_ATTRIBUTES = ('aria-label', 'placeholder', 'name', 'title', 'alt', 'id', 'type')

# Parameter names never worth remembering, either because they are per-run noise
# (element indices change between visits) or because they carry user input.
_SKIP_PARAMS = frozenset({'index', 'interacted_element', 'text', 'value', 'password', 'arguments', 'script'})


def domain_of(url: str | None) -> str:
	"""Registrable host of a URL, used as the memory key."""
	if not url:
		return ''
	try:
		return (urlparse(url).hostname or '').lower()
	except ValueError:
		return ''


class WorkflowMemory:
	"""Remembers routes that worked, keyed by site.

	Induction is deterministic rather than LLM-driven: it compacts the actions the agent
	actually took. That costs nothing per run, is reproducible, and — unlike asking a model
	to summarize its own trajectory — cannot invent a step that never happened.
	"""

	def __init__(self, path: Path | str | None = None, enabled: bool = True) -> None:
		self.enabled = enabled
		self.path = Path(path).expanduser() if path else self._default_path()
		self._workflows: list[Workflow] = []
		self._loaded = False

	@staticmethod
	def _default_path() -> Path:
		from browser_use.config import CONFIG

		return CONFIG.BROWSER_USE_CONFIG_DIR / 'workflows.json'

	# -- persistence ---------------------------------------------------------------

	def load(self) -> None:
		"""Read stored workflows. A corrupt or unreadable file is not fatal — memory is an
		optimization, and losing it must never stop a run."""
		if self._loaded:
			return
		self._loaded = True
		if not self.path.exists():
			return
		try:
			raw = json.loads(self.path.read_text())
		except (OSError, json.JSONDecodeError) as e:
			logger.debug(f'🧠 Could not read workflow memory at {self.path}: {type(e).__name__}: {e}')
			return
		if not isinstance(raw, list):
			return
		for entry in raw:
			try:
				self._workflows.append(Workflow.model_validate(entry))
			except ValidationError:
				continue

	def save(self) -> None:
		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)
			self.path.write_text(json.dumps([w.model_dump(mode='json') for w in self._workflows], indent=1))
		except OSError as e:
			logger.debug(f'🧠 Could not write workflow memory to {self.path}: {type(e).__name__}: {e}')

	@property
	def workflows(self) -> list[Workflow]:
		self.load()
		return list(self._workflows)

	# -- induction -----------------------------------------------------------------

	@staticmethod
	def _label_for(interacted_element: Any) -> str:
		"""A stable, human-readable handle for the element an action touched.

		Deliberately not the element index: indices are assigned per snapshot and mean
		nothing on the next visit, so remembering "click 17" is worse than useless.
		"""
		if interacted_element is None:
			return ''
		attributes = getattr(interacted_element, 'attributes', None)
		if attributes is None and isinstance(interacted_element, dict):
			attributes = interacted_element.get('attributes')
		node_value = getattr(interacted_element, 'node_value', None)
		if node_value is None and isinstance(interacted_element, dict):
			node_value = interacted_element.get('node_value')

		if isinstance(attributes, dict):
			for key in _LABEL_ATTRIBUTES:
				if value := attributes.get(key):
					return str(value)
		return str(node_value or '')

	@classmethod
	def _step_from_action(cls, entry: dict) -> WorkflowStep | None:
		names = [key for key in entry if key != 'interacted_element']
		if not names:
			return None
		action = names[0]
		if action in UNINFORMATIVE_ACTIONS:
			return None

		params = entry.get(action) or {}
		if not isinstance(params, dict):
			params = {}

		label = cls._label_for(entry.get('interacted_element'))

		if action in VALUE_BEARING_ACTIONS:
			# The action is the lesson; the value may be a password.
			return WorkflowStep(action=action, detail=f'into {label}' if label else '')

		if url := params.get('url'):
			return WorkflowStep(action=action, detail=str(url))
		if query := params.get('query'):
			return WorkflowStep(action=action, detail=str(query))
		if label:
			return WorkflowStep(action=action, detail=label)

		remainder = {k: v for k, v in params.items() if k not in _SKIP_PARAMS}
		detail = ', '.join(f'{k}={v}' for k, v in list(remainder.items())[:3])
		return WorkflowStep(action=action, detail=detail)

	def record(self, task: str, history: 'AgentHistoryList') -> Workflow | None:
		"""Induce a workflow from a run, if it is worth remembering.

		Only successful runs are stored: a failed trajectory is a route that does not work,
		and replaying it would actively mislead the next attempt.
		"""
		if not self.enabled:
			return None
		if history.is_successful() is not True:
			return None

		urls = [url for url in history.urls() if url]
		domain = domain_of(urls[0] if urls else None)
		if not domain:
			return None

		steps: list[WorkflowStep] = []
		for entry in history.model_actions():
			if len(steps) >= MAX_STEPS_PER_WORKFLOW:
				break
			if step := self._step_from_action(entry):
				steps.append(step)
		if not steps:
			return None

		self.load()
		workflow = Workflow(domain=domain, task=task, steps=steps)
		self._workflows.append(workflow)
		self._prune(domain)
		self.save()
		logger.debug(f'🧠 Remembered a {len(steps)}-step route on {domain}')
		return workflow

	def _prune(self, domain: str) -> None:
		"""Keep the most recent routes per domain, drop the rest."""
		same_domain = [w for w in self._workflows if w.domain == domain]
		if len(same_domain) <= MAX_WORKFLOWS_PER_DOMAIN:
			return
		keep = set(id(w) for w in sorted(same_domain, key=lambda w: w.created_at, reverse=True)[:MAX_WORKFLOWS_PER_DOMAIN])
		self._workflows = [w for w in self._workflows if w.domain != domain or id(w) in keep]

	# -- recall --------------------------------------------------------------------

	def recall(self, task: str, url: str | None, limit: int = 2) -> list[Workflow]:
		"""The most relevant remembered routes for this task on this site."""
		if not self.enabled:
			return []
		assert limit > 0, 'recall() limit must be positive'
		self.load()
		domain = domain_of(url)
		if not domain:
			return []
		scored = [(w.score(task, domain), w) for w in self._workflows]
		hits = [w for score, w in sorted(scored, key=lambda pair: pair[0], reverse=True) if score > 0]
		return hits[:limit]

	def describe(self, task: str, url: str | None, limit: int = 2) -> str:
		"""The prompt block for this task, or '' when nothing is remembered."""
		recalled = self.recall(task, url, limit=limit)
		for workflow in recalled:
			workflow.uses += 1
		return render_workflow_memory(recalled)
