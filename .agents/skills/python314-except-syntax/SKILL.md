---
name: python314-except-syntax
description: Use when reviewing, linting, or writing Python exception handlers in this repo — paren-less multi-exception `except A, B:` is valid here and must not be "fixed"
---

# Paren-less `except A, B:` is valid in this repo

This project targets Python 3.14+ (`pyproject.toml`: `requires-python = '>=3.14'`).
PEP 758 (Python 3.14) allows multiple exception types in an `except` clause
**without parentheses**:

```python
try:
    graph.pull()
except av.FFmpegError, EOFError:  # valid on Python 3.14+
    break
```

Do NOT:

- report it as Python 2 syntax or a `SyntaxError`
- suggest wrapping the tuple in parentheses "for compatibility"
- rewrite it during unrelated refactors

Parenthesized form `except (A, B):` is also fine — both are acceptable; just
don't flag either style as an error. Note this only holds because the floor is
3.14; if `requires-python` ever drops below 3.14, paren-less handlers become a
real syntax error.
