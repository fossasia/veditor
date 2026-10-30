---
name: sqlalchemy-2x
description: Use when writing or editing any SQLAlchemy database code (queries, inserts, updates, deletes) to keep it type-safe in the 2.x style instead of the deprecated legacy Session.query API
---

# SQLAlchemy 2.x style

Never use the legacy `Session.query()` API — it is deprecated and unsound under
type checkers (e.g. `db.execute()` returns `Result`, which has no `rowcount`,
forcing unsafe `cast()` calls).

## Queries

`.query(...).filter(...).first()` → `db.scalar(select(...).where(...))`
(`Result.scalar()` returns the first row's first entity, `None` if no rows —
exact equivalent of `.first()`):

```python
from sqlalchemy import select

user = db.scalar(select(models.User).where(models.User.email == email))
```

- `.all()` → `db.scalars(select(...)).all()`
- `count()` → `select(func.count()).select_from(...)` via `db.scalar(...)`
- `.first()` on a non-unique column → `db.scalars(select(...)).first()`
- No filter: `db.scalars(select(models.Event)).all()`

## Updates / Deletes

Use Core-style statements and RETURNING to get a typed result instead of
`rowcount` + `cast(CursorResult, ...)`:

```python
result = db.execute(
    update(models.User)
    .where(models.User.id == user.id, models.User.role == AuthRole.USER)
    .values(role=AuthRole.ORGANIZER)
    .returning(models.User.id)
)
updated_id = result.scalar_one_or_none()
db.commit()
if updated_id is None:
    ...
```

Same for `.delete()`: `db.execute(delete(models.X).where(...))`.

Never import `CursorResult` or use `cast(...)` to reach `rowcount`; use
`.returning()` + `scalar_one_or_none()` (or `scalar_one()` when exactly one row
must be affected).

## Model instances

Prefer loading entities and mutating attributes (`user.role = AuthRole.SPEAKER`
followed by `db.commit()`), or use `Session.get(models.User, id)` for
primary-key lookups.

## Enums over string literals

Use shared enum members (e.g. `AuthRole.USER` from `app.consts`) instead of
raw string literals in filters and values — `StrEnum` compares equal to the
stored string, so this stays type-safe and typo-proof.

## Testing SQLAlchemy code

Tests must use the `db_session` fixture from `tests/conftest.py` (real database
wrapped in a per-test transaction) instead of mocking `Session.query()` /
`Session.scalar()` — mocked sessions hide query mistakes and break on every
query-style change. Create real rows with model instances and let the routes
run against the fixture session.

```python
def test_get_talk(db_session):
    event = models.Event(name="Test Event")
    db_session.add(event)
    db_session.commit()
    talk = models.Talk(event_id=event.id, title="T", start=..., end=...)
    db_session.add(talk)
    db_session.commit()
    ...
```

Rules:

- Do not mock `Session` methods (`query`, `scalar`, `scalars`, `execute`,
  `commit`, `flush`, `refresh`) to fake query results; that couples tests to
  the legacy API and silently passes wrong queries.
- Keep dependency overrides only for non-DB dependencies (auth identity,
  storage backend). `db_session` already overrides `get_db`.
- For background job functions that open their own session via
  `SessionLocal()`, bind worker sessions to the test connection:
  `TestSessionLocal(bind=db_session.get_bind())` and patch `SessionLocal`
  with a factory returning such sessions, then `db_session.expire_all()`
  before asserting persisted state.
- Failure simulation (commit/flush errors, IntegrityError races) is the one
  acceptable place to mock: patch the session method or use a
  `sqlalchemy.event` listener (`before_commit` / `before_flush`).
