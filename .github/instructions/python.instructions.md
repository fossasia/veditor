---
applyTo: "**/*.py"
---

# Python Instructions

## Imports

- Do not import inside a function. All imports must be at module top level.
- The only exception is when an in-function import is required to avoid a circular import. In that case, add a brief comment explaining the circular dependency being avoided.

## Enumerations (`StrEnum`)

- Prefer `enum.StrEnum` over raw string literals for closed sets of domain values (statuses, roles, scopes). Define each canonical vocabulary once, for example `TalkStatus` or `AuthRole`, and use its members everywhere instead of repeating strings.
- Since members are strings, comparisons, dict keys, and serialization against raw strings keep working. Still pass members for function parameters, return values, and comparisons to stay type-safe.
- Do not type parameters or attributes as `str` when a `StrEnum` member exists. Type them as the enum so invalid values are caught statically. If raw strings arrive from outside the codebase (request payloads, task messages), convert at the boundary with `Status(value)` and handle the `ValueError`.

## Type-safe attribute access (no `getattr`)

- Do not use `getattr(obj, "attr", default)` when the attribute is statically known to exist. Use direct attribute access (`obj.attr`) instead, combined with `isinstance` checks, `None` checks, or `try/except AttributeError` when the value may be absent at runtime.
- `getattr` is only acceptable for genuinely dynamic attribute names (for example, settings resolved from a runtime string key).
- When a function receives an `object` or a loose union and needs specific attributes, narrow it with `isinstance`, a structural `Protocol`, or a proper union type instead of reaching for `getattr`.

## Exception handling (no broad `except Exception`)

- Do not catch `Exception` (or `BaseException`) in `except` clauses. Catch the most specific exception types the operation can actually raise, for example `SQLAlchemyError` for database work, `redis.exceptions.RedisError` for Redis and RQ operations, `(OSError, ValueError)` for storage or file I/O, `(urllib.error.URLError, http.client.HTTPException)` for HTTP requests, and `smtplib.SMTPException` for email.
- Do not add `# noqa: BLE001` (or similar suppressions for broad-except linters); the code must satisfy the rule itself.
- Re-raise deliberately when the caller must decide what to do: use `except Exception:` only in the rare pattern `except Exception: <cleanup>; raise`, and never swallow silently without logging.
- Tests that simulate failures must raise the same exception types the production handlers catch (for example `RedisError` instead of a bare `Exception` or `RuntimeError` mock).

## Naming

- Prefer unqualified type names in annotations and casts (for example, `Request` instead of `urllib.request.Request`); import the type at module top level.
- If two imports would collide (for example FastAPI's `Request` and `urllib.request.Request` in the same module), keep the more domain-relevant one (FastAPI's `Request`) under its plain name and alias the other (for example, `from urllib.request import Request as URLRequest`).
- In a `cast()` call with a subscripted generic (for example `Packet[VideoStream]`), pass the type as a string so it is not evaluated at runtime: `cast("Packet[VideoStream]", value)`.
