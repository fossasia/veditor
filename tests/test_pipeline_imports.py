import ast
from pathlib import Path

FORBIDDEN_PREFIXES = ("fastapi", "rq", "sqlalchemy", "app.db")


def is_forbidden(module_name: str | None) -> bool:
    if not module_name:
        return False
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in FORBIDDEN_PREFIXES
    )


def test_pipeline_imports():
    """
    Ensure that no file inside app/pipeline imports from forbidden modules:
    fastapi, rq, sqlalchemy, or app.db.
    """
    project_root = Path(__file__).parent.parent
    pipeline_dir = project_root / "app" / "pipeline"

    assert pipeline_dir.is_dir(), (
        "app/pipeline directory is missing; import guard cannot run"
    )

    violations = []

    for py_file in pipeline_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            rel_path = py_file.relative_to(project_root)
            raise AssertionError(f"Failed to parse {rel_path}: {exc}") from exc

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if is_forbidden(alias.name):
                        violations.append(
                            f"{py_file.relative_to(project_root)} imports {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""

                # Resolve relative imports from inside app/pipeline
                if node.level >= 2:  # e.g., from .. import db
                    module_name = f"app.{module_name}" if module_name else "app"

                for alias in node.names:
                    full_name = (
                        f"{module_name}.{alias.name}" if module_name else alias.name
                    )

                    if is_forbidden(module_name) or is_forbidden(full_name):
                        source_module = node.module or "relative"
                        violations.append(
                            f"{py_file.relative_to(project_root)} imports {alias.name} from {source_module}"
                        )

    if violations:
        violation_msg = "\n".join(violations)
        raise AssertionError(
            f"Forbidden imports found in pipeline module:\n{violation_msg}"
        )
