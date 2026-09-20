"""NVIDIA NIM: the hosted catalog at integrate.api.nvidia.com, and self-hosted containers.

NIM speaks the OpenAI chat-completions schema, so this is a thin configuration layer over
`ChatOpenAI` rather than a new client. What it actually buys you:

- the right `base_url` by default, so `ChatNVIDIANIM(model=...)` just works
- `NVIDIA_API_KEY` (or `NVIDIA_NIM_API_KEY`) instead of silently falling back to
  `OPENAI_API_KEY`, which is the failure mode of pointing plain ChatOpenAI at NIM: the
  OpenAI SDK finds *an* key in the environment and sends it to NVIDIA, which 401s in a
  way that reads like a NIM outage rather than a misconfiguration
- a `base_url` override for self-hosted NIM containers, which serve the same schema on
  a local port

Structured output: browser-use drives the agent through a JSON schema, so pick a model
whose NIM deployment supports `response_format`. Most instruct models in the catalog do;
if one does not, the agent step fails schema validation rather than misbehaving quietly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from browser_use.llm.openai.chat import ChatOpenAI

# The hosted catalog. Self-hosted NIM containers expose the same routes on their own host,
# so pointing base_url at http://localhost:8000/v1 is the only change needed.
NVIDIA_NIM_BASE_URL = 'https://integrate.api.nvidia.com/v1'

# Checked in order. NVIDIA's own tooling uses NVIDIA_API_KEY; the second name is accepted
# because it is what people reach for when they run several NVIDIA services side by side.
NVIDIA_API_KEY_ENV_VARS = ('NVIDIA_API_KEY', 'NVIDIA_NIM_API_KEY')


@dataclass
class ChatNVIDIANIM(ChatOpenAI):
	"""NVIDIA NIM chat model (OpenAI-compatible).

	```python
	from browser_use import Agent
	from browser_use.llm import ChatNVIDIANIM

	llm = ChatNVIDIANIM(model='meta/llama-3.3-70b-instruct')  # reads NVIDIA_API_KEY
	agent = Agent(task='...', llm=llm)
	```

	Self-hosted container:

	```python
	llm = ChatNVIDIANIM(model='meta/llama-3.3-70b-instruct', base_url='http://localhost:8000/v1', api_key='not-needed')
	```
	"""

	model: str = 'meta/llama-3.3-70b-instruct'
	base_url: str | httpx.URL | None = NVIDIA_NIM_BASE_URL

	def __post_init__(self) -> None:
		# Resolve the key here rather than leaving it to the OpenAI SDK, whose fallback is
		# OPENAI_API_KEY — a key that is valid-looking, wrong, and confusing to debug.
		if self.api_key is None:
			for env_var in NVIDIA_API_KEY_ENV_VARS:
				value = os.getenv(env_var)
				if value:
					self.api_key = value
					break
		# A self-hosted container usually wants no auth at all, but the OpenAI SDK refuses
		# to construct a client without *some* key, so give it a placeholder.
		if self.api_key is None and self._is_local_base_url():
			self.api_key = 'not-needed'

		post_init = getattr(super(), '__post_init__', None)
		if callable(post_init):
			post_init()

	def _is_local_base_url(self) -> bool:
		base_url = str(self.base_url or '')
		return any(host in base_url for host in ('localhost', '127.0.0.1', '0.0.0.0', '[::1]'))

	@property
	def provider(self) -> str:
		return 'nvidia_nim'

	@property
	def name(self) -> str:
		return self.model
