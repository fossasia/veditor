---
applyTo: "**/*.py"
---

# Python Instructions

## Imports

- Do not import inside a function. All imports must be at module top level.
- The only exception is when an in-function import is required to avoid a circular import. In that case, add a brief comment explaining the circular dependency being avoided.

## Type-safe attribute access (no `getattr`)

- Do not use `getattr(obj, "attr", default)` when the attribute is statically known to exist. Use direct attribute access (`obj.attr`) instead, combined with `isinstance` checks, `None` checks, or `try/except AttributeError` when the value may be absent at runtime.
- `getattr` is only acceptable for genuinely dynamic attribute names (for example, settings resolved from a runtime string key).
- When a function receives an `object` or a loose union and needs specific attributes, narrow it with `isinstance`, a structural `Protocol`, or a proper union type instead of reaching for `getattr`.

## Naming

- Prefer unqualified type names in annotations and casts (for example, `Request` instead of `urllib.request.Request`); import the type at module top level.
- If two imports would collide (for example FastAPI's `Request` and `urllib.request.Request` in the same module), keep the more domain-relevant one (FastAPI's `Request`) under its plain name and alias the other (for example, `from urllib.request import Request as URLRequest`).
- In a `cast()` call with a subscripted generic (for example `Packet[VideoStream]`), pass the type as a string so it is not evaluated at runtime: `cast("Packet[VideoStream]", value)`.
