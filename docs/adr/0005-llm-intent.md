# ADR-0005: Anthropic Messages API for intent extraction, validated deterministically

**Status:** accepted · 2026-09-29

The LLM extracts; it never decides. A single forced tool call (`tool_choice: record_purchase_intent`)
returns a JSON object; `IntentParser` then:
* validates it with pydantic (`extra="forbid"`, bounds on quantity/price),
* requires any price ceiling to literally appear in the user's text (hallucinated or dropped
  limits become a clarifying question),
* maps "no vendor" to the single allow-listed vendor or asks which one,
* rejects unsupported categories with an explanation.

The prompt wraps the user text in `<request>` delimiters and tells the model to treat it as
data. The model sees only the request and the lists of supported vendor/category ids — never
addresses, payment data, or caps. Model is configurable (`SPENDDAT_LLM_MODEL`); tests use a
scripted stand-in.
