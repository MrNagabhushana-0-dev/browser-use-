"""
Regression test for a SchemaOptimizer bug where free-form `dict`/`dict[str, X]`
fields were silently corrupted into a schema that can never hold any data.

`SchemaOptimizer.create_optimized_json_schema` is shared by every LLM provider
(OpenAI, Anthropic, Google, Groq, DeepSeek, Mistral, etc.) to build the JSON
schema sent for structured/tool-call output. Pydantic represents a free-form
mapping field (`dict[str, Any]`, or a schema-derived model field built by
`browser_use/tools/extraction/schema_utils.py` for a user-supplied
`{"type": "object"}` extraction field with no nested `properties`) as:

    {"type": "object", "additionalProperties": true, ...}

The optimizer's blanket "add additionalProperties: false to every object" pass
(both in `optimize_schema` and in the later `ensure_additional_properties_false`
sweep) dropped the original `additionalProperties` value unconditionally and
replaced it with `False`, while never adding any `properties` for a bare dict
field. The result, `{"type": "object", "additionalProperties": false}` with no
`properties`, is only satisfiable by `{}` under JSON Schema semantics: any real
key the model tries to report for that field becomes invalid, so providers that
enforce the schema (Anthropic tool-use, OpenAI strict structured output, Gemini,
etc.) can never actually return non-empty data for it. This is a silent data-loss
bug, not a hypothetical: `browser_use/tools/extraction/schema_utils.py` builds
exactly this kind of bare-dict field whenever a user's custom extraction schema
asks for an arbitrary-keys object (e.g. `{"type": "object"}` with no
`properties`, a common way to ask for "extract these as key/value pairs").
"""

from typing import Any

from pydantic import BaseModel

from browser_use.llm.schema import SchemaOptimizer


class ModelWithFreeformDict(BaseModel):
	"""Mirrors what `schema_dict_to_pydantic_model` builds for a user-supplied
	extraction schema field of `{"type": "object"}` with no nested properties."""

	metadata: dict[str, Any] = {}
	title: str = ''


def test_optimizer_preserves_additional_properties_for_freeform_dict_field():
	"""A bare `dict[str, Any]` field must remain able to hold arbitrary keys.

	Before the fix, this field's `additionalProperties` was forced to `False`
	with no `properties`, making the field only ever satisfiable by `{}` --
	any real extracted data for it would violate the schema.
	"""
	schema = SchemaOptimizer.create_optimized_json_schema(ModelWithFreeformDict)

	metadata_schema = schema['properties']['metadata']
	assert metadata_schema['type'] == 'object'

	# A field with no defined `properties` must not have additionalProperties
	# forced to False -- that would make the field satisfiable only by `{}`.
	assert not ('properties' not in metadata_schema and metadata_schema.get('additionalProperties') is False), (
		f'Free-form dict field was corrupted into an always-empty object: {metadata_schema!r}. '
		'A real value like {"sku": "A-1"} would fail schema validation.'
	)

	# Sanity: modeled (non-dict) top-level object must still be closed for
	# strict-mode compatibility -- this must NOT regress.
	assert schema.get('additionalProperties') is False
	assert 'title' in schema['properties']
