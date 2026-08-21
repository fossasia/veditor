import ast
from pathlib import Path

EXEMPTION_MARKER = "# storage-boundary-exempt:"

# Function names from modules or builtins that perform direct file I/O
FORBIDDEN_BUILTINS = {"open"}
FORBIDDEN_SHUTIL_FUNCS = {
    "copy",
    "copy2",
    "copyfile",
    "copyfileobj",
    "copymode",
    "copystat",
    "copytree",
    "move",
    "rmtree",
}
FORBIDDEN_OS_FUNCS = {
    "remove",
    "unlink",
    "rename",
    "replace",
    "rmdir",
    "removedirs",
    "mkdir",
    "makedirs",
}
FORBIDDEN_PATH_METHODS = {
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
    "unlink",
    "rmdir",
    "mkdir",
    "open",
}


def is_exempt(lines: list[str], lineno: int) -> bool:
    """Check if the line or the line immediately preceding it has an exemption marker."""
    if 1 <= lineno <= len(lines):
        # Check current line
        if EXEMPTION_MARKER in lines[lineno - 1]:
            return True
        # Check preceding line if comment is placed immediately above
        if lineno > 1:
            prev_line = lines[lineno - 2].strip()
            if prev_line.startswith(EXEMPTION_MARKER):
                return True
    return False


def find_storage_violations(
    source_code: str, file_path: str = "<unknown>"
) -> list[str]:
    """
    Parse Python source code AST and find any unexempted direct filesystem/IO calls.
    Returns a list of human-readable violation messages.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        return [f"{file_path}:{e.lineno}: SyntaxError while parsing: {e}"]

    lines = source_code.splitlines()
    violations: list[str] = []

    # Track imported names from forbidden modules (e.g., `from shutil import copyfile`)
    imported_forbidden: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "shutil":
                for alias in node.names:
                    if alias.name in FORBIDDEN_SHUTIL_FUNCS:
                        imported_forbidden[alias.asname or alias.name] = (
                            f"shutil.{alias.name}"
                        )
            elif node.module == "os":
                for alias in node.names:
                    if alias.name in FORBIDDEN_OS_FUNCS:
                        imported_forbidden[alias.asname or alias.name] = (
                            f"os.{alias.name}"
                        )
            elif node.module in ("io", "_io"):
                for alias in node.names:
                    if alias.name == "open":
                        imported_forbidden[alias.asname or alias.name] = "io.open"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        lineno = getattr(node, "lineno", 0)
        if is_exempt(lines, lineno):
            continue

        func = node.func

        # Case 1: Direct function calls like `open(...)` or imported `copyfile(...)`
        if isinstance(func, ast.Name):
            if func.id in FORBIDDEN_BUILTINS:
                violations.append(
                    f"{file_path}:{lineno}: Direct call to builtin '{func.id}()'. All file I/O must go through storage.py"
                )
            elif func.id in imported_forbidden:
                orig_name = imported_forbidden[func.id]
                violations.append(
                    f"{file_path}:{lineno}: Direct call to '{orig_name}()' (via '{func.id}'). All file I/O must go through storage.py"
                )

        # Case 2: Attribute calls like `shutil.copy(...)`, `os.remove(...)`, `io.open(...)`, `Path(...).write_text(...)`
        elif isinstance(func, ast.Attribute):
            attr_name = func.attr

            # Check `shutil.<func>` or `os.<func>` or `io.open`
            if isinstance(func.value, ast.Name):
                module_name = func.value.id
                if module_name == "shutil" and attr_name in FORBIDDEN_SHUTIL_FUNCS:
                    violations.append(
                        f"{file_path}:{lineno}: Direct call to 'shutil.{attr_name}()'. All file I/O must go through storage.py"
                    )
                elif module_name == "os" and attr_name in FORBIDDEN_OS_FUNCS:
                    violations.append(
                        f"{file_path}:{lineno}: Direct call to 'os.{attr_name}()'. All file I/O must go through storage.py"
                    )
                elif module_name in ("io", "_io") and attr_name == "open":
                    violations.append(
                        f"{file_path}:{lineno}: Direct call to 'io.open()'. All file I/O must go through storage.py"
                    )

            # Check `Path(...).<method>()`
            if attr_name in FORBIDDEN_PATH_METHODS:
                violations.append(
                    f"{file_path}:{lineno}: Direct call to Path method '.{attr_name}()'. All file I/O must go through storage.py"
                )

    return violations


def test_app_storage_boundary():
    """
    Ensure that no file inside app/ (excluding app/storage.py) performs
    direct filesystem I/O operations without an explicit exemption marker.
    """
    project_root = Path(__file__).parent.parent
    app_dir = project_root / "app"
    storage_file = (app_dir / "storage.py").resolve()

    assert app_dir.is_dir(), "app directory is missing"

    all_violations: list[str] = []

    for py_file in sorted(app_dir.rglob("*.py")):
        if py_file.resolve() == storage_file:
            continue

        source_code = py_file.read_text(encoding="utf-8")
        rel_path = str(py_file.relative_to(project_root))
        violations = find_storage_violations(source_code, file_path=rel_path)
        all_violations.extend(violations)

    if all_violations:
        violation_report = "\n".join(all_violations)
        raise AssertionError(
            f"Storage boundary violations found outside app/storage.py:\n{violation_report}\n\n"
            f"If this is a legitimate non-media file operation (e.g. config loading), add a comment:\n"
            f"# storage-boundary-exempt: <reason>"
        )


def test_storage_boundary_detects_violations():
    """
    Regression test: Verify that deliberate direct file I/O operations are detected.
    """
    violating_snippets = """
from pathlib import Path
import shutil
from os import remove

def do_forbidden():
    with open("sample.txt", "w") as f:
        f.write("hello")

    shutil.copy("a.mp4", "b.mp4")
    shutil.rmtree("/tmp/folder")
    remove("old.mp4")

    p = Path("video.mp4")
    p.write_bytes(b"123")
    p.read_text()
    p.unlink()
"""
    violations = find_storage_violations(violating_snippets, file_path="fixture.py")

    assert any("open()" in v for v in violations), "Should catch open()"
    assert any("shutil.copy()" in v for v in violations), "Should catch shutil.copy()"
    assert any("shutil.rmtree()" in v for v in violations), (
        "Should catch shutil.rmtree()"
    )
    assert any("os.remove()" in v for v in violations), "Should catch imported remove()"
    assert any(".write_bytes()" in v for v in violations), (
        "Should catch Path.write_bytes()"
    )
    assert any(".read_text()" in v for v in violations), "Should catch Path.read_text()"
    assert any(".unlink()" in v for v in violations), "Should catch Path.unlink()"


def test_storage_boundary_respects_exemptions():
    """
    Verify that operations annotated with `# storage-boundary-exempt: <reason>` are permitted.
    """
    exempted_snippet = """
from pathlib import Path

def load_config():
    # storage-boundary-exempt: reading static configuration file
    with open("config.json", "r") as f:
        data = f.read()

    # Preceding line exemption
    # storage-boundary-exempt: writing debug crash log
    Path("/tmp/crash.log").write_text("crash")

    # Same line exemption
    Path("/tmp/log.txt").read_text()  # storage-boundary-exempt: reading log text
"""
    violations = find_storage_violations(
        exempted_snippet, file_path="exempted_fixture.py"
    )
    assert violations == [], (
        f"Expected 0 violations for exempted code, got: {violations}"
    )
