# ADR-0007: tool schemas compile to strict Ollama `format`

Status: ACCEPTED · Phase 3

Tools are functions decorated with @tool(name, description, JSON schema).
`ToolSpec.strict_format()` normalizes each schema into a strict object
grammar (`additionalProperties: false`) that plugs directly into Ollama's
`format=` parameter — grammar-constrained decoding without a GBNF compiler.
Discovery imports every non-underscore module in a directory (built-ins in
`builtin_tools.py`; operators can point discovery at their own folder).