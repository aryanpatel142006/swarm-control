"""JSON schemas in the form OpenAI structured outputs accept in strict mode.

Newer Codex CLIs send `--output-schema` to the Responses API with `strict: true` (codex-c on laptop-c, Oct 10).
Strict mode requires every object to set `"additionalProperties": false` and to list every key of `properties` in
`required`; an optional field must instead accept null. Older CLIs (laptop-b) accept the strict form as well, so the
Codex adapter always sends it. Parsers treat a null field the same as a missing one (`drop_nulls`)."""
from __future__ import annotations

import copy

# keywords strict mode rejects (or that older docs list as unsupported); dropped wherever they appear
UNSUPPORTED = frozenset({
    "default", "examples", "minLength", "maxLength", "patternProperties", "propertyNames", "minProperties",
    "maxProperties", "unevaluatedProperties", "unevaluatedItems", "dependentRequired", "dependentSchemas",
    "if", "then", "else", "not", "contains", "minContains", "maxContains", "uniqueItems", "$schema", "$id",
})
_SCALARS = {"string", "integer", "number", "boolean"}


def to_openai_strict(schema: dict) -> dict:
    """A deep copy of `schema` that satisfies OpenAI strict structured outputs: every object closed and with all
    properties required, previously optional properties made nullable, `oneOf` turned into `anyOf`, unsupported
    keywords dropped. Descriptions, enums and `$ref`/`$defs` are kept. The root stays an object."""
    return _node(copy.deepcopy(schema))


def _node(s):
    if not isinstance(s, dict):
        return s
    s = {k: v for k, v in s.items() if k not in UNSUPPORTED}
    if "oneOf" in s:
        s["anyOf"] = (s.pop("oneOf") or []) + list(s.get("anyOf") or [])
    if "anyOf" in s:
        s["anyOf"] = [_node(x) for x in s["anyOf"]]
    if "allOf" in s:
        s["allOf"] = [_node(x) for x in s["allOf"]]
    for defs in ("$defs", "definitions"):
        if isinstance(s.get(defs), dict):
            s[defs] = {k: _node(v) for k, v in s[defs].items()}
    if "items" in s:
        s["items"] = _node(s["items"])
    types = s.get("type")
    is_object = types == "object" or (isinstance(types, list) and "object" in types) or "properties" in s
    if is_object:
        props = s.get("properties") or {}
        was_required = set(s.get("required") or [])
        s["properties"] = {name: (_node(p) if name in was_required else _nullable(_node(p)))
                           for name, p in props.items()}
        s["required"] = list(s["properties"])
        s["additionalProperties"] = False
        if "type" not in s:
            s["type"] = "object"
    return s


def _nullable(s: dict) -> dict:
    """`s` that also accepts null: a scalar type gains "null" (its enum gains None); anything else (objects,
    arrays, $ref, anyOf, untyped) is wrapped as anyOf [s, {"type": "null"}] with the description kept outside."""
    if not isinstance(s, dict):
        return s
    if _accepts_null(s):
        return s
    t = s.get("type")
    ts = [t] if isinstance(t, str) else (list(t) if isinstance(t, list) else [])
    if ts and set(ts) <= _SCALARS and "$ref" not in s and "anyOf" not in s:
        out = dict(s, type=ts + ["null"])
        if isinstance(out.get("enum"), list):
            out["enum"] = list(out["enum"]) + [None]
        return out
    if "anyOf" in s and set(s) <= {"anyOf", "description", "title"}:
        return dict(s, anyOf=list(s["anyOf"]) + [{"type": "null"}])
    inner = {k: v for k, v in s.items() if k not in ("description", "title")}
    out = {k: s[k] for k in ("description", "title") if k in s}
    out["anyOf"] = [inner, {"type": "null"}]
    return out


def _accepts_null(s: dict) -> bool:
    t = s.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    return any(isinstance(x, dict) and _accepts_null(x) for x in (s.get("anyOf") or []))


def strict_violations(schema, path: str = "$") -> list[str]:
    """Every place `schema` breaks the strict rules (empty when it complies): an object that is not closed or
    leaves a property out of `required`, a `oneOf`, or an unsupported keyword."""
    out: list[str] = []
    if not isinstance(schema, dict):
        return out
    for k in schema:
        if k in UNSUPPORTED:
            out.append(f"{path}: unsupported keyword {k}")
    if "oneOf" in schema:
        out.append(f"{path}: oneOf")
    t = schema.get("type")
    if t == "object" or (isinstance(t, list) and "object" in t) or "properties" in schema:
        if schema.get("additionalProperties") is not False:
            out.append(f"{path}: additionalProperties is not false")
        props = schema.get("properties")
        if not isinstance(props, dict):
            out.append(f"{path}: object without properties")
            props = {}
        missing = set(props) - set(schema.get("required") or [])
        if missing:
            out.append(f"{path}: not required: {sorted(missing)}")
        for name, p in props.items():
            out += strict_violations(p, f"{path}.{name}")
    if "items" in schema:
        out += strict_violations(schema["items"], f"{path}[]")
    for key in ("anyOf", "allOf"):
        for i, x in enumerate(schema.get(key) or []):
            out += strict_violations(x, f"{path}.{key}[{i}]")
    for defs in ("$defs", "definitions"):
        for name, x in (schema.get(defs) or {}).items():
            out += strict_violations(x, f"{path}.{defs}.{name}")
    return out


def drop_nulls(value):
    """`value` with every null-valued dict key removed, recursively: a strict-mode reply fills each optional field
    it did not use with null, and the parsers read that the same as a missing field."""
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value if v is not None]
    return value
