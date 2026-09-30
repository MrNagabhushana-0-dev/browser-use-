"""What an exploration finds: findings, per-page reports, and the whole run."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal['high', 'medium', 'low', 'info']
SEVERITY_ORDER: dict[str, int] = {'high': 0, 'medium': 1, 'low': 2, 'info': 3}


class Finding(BaseModel):
	"""One problem, with every page it was seen on."""

	model_config = ConfigDict(extra='forbid')

	kind: str
	severity: Severity
	title: str
	detail: str = ''
	pages: list[str] = Field(default_factory=list)
	evidence: list[str] = Field(default_factory=list)

	@property
	def key(self) -> tuple[str, str]:
		return (self.kind, self.title)


class PageReport(BaseModel):
	model_config = ConfigDict(extra='forbid')

	url: str
	status: int | None = None
	title: str = ''
	seconds: float = 0.0  # wall time spent on the page, including scrolling and checks
	navigation_seconds: float = 0.0
	screens_scrolled: int = 0
	probe: dict[str, Any] = Field(default_factory=dict)
	mobile: dict[str, Any] = Field(default_factory=dict)
	console: list[str] = Field(default_factory=list)
	failed_requests: list[str] = Field(default_factory=list)
	digest_tokens: int = 0
	image_tokens: int = 0
	dom_state_tokens: int | None = None  # what the classic DOM-dump step would cost here
	error: str = ''


class ExploreReport(BaseModel):
	model_config = ConfigDict(extra='forbid')

	start_url: str
	pages: list[PageReport] = Field(default_factory=list)
	findings: list[Finding] = Field(default_factory=list)
	skipped: list[str] = Field(default_factory=list)  # disallowed by robots.txt or over the page cap
	link_status: dict[str, int] = Field(default_factory=dict)
	seconds: float = 0.0
	environment: dict[str, Any] = Field(default_factory=dict)

	@property
	def tokens(self) -> int:
		return sum(p.digest_tokens + p.image_tokens for p in self.pages)

	@property
	def dom_state_tokens(self) -> int:
		return sum(p.dom_state_tokens or 0 for p in self.pages)
