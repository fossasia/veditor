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
    "open",
    "remove",
    "unlink",
    "rename",
    "replace",
    "rmdir",
    "removedirs",
    "mkdir",
    "makedirs",
}
# Method names commonly associated with pathlib.Path file operations.
# Note: As an AST heuristic, these are flagged on any attribute call matching these names.
# If custom non-Path classes implement methods with these names (e.g. read_text, write_bytes),
# annotate those call sites with `# storage-boundary-exempt: <reason>`.
FORBIDDEN_PATH_METHODS = {
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
    "unlink",
    "rmdir",
    "mkdir",
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

    # Track module aliases (e.g., `import shutil as sh` -> `{"sh": "shutil"}`)
    module_aliases: dict[str, str] = {}
    # Track imported names from forbidden modules (e.g., `from shutil import copyfile`)
    imported_forbidden: dict[str, str] = {}
    # Track Path imports and classes (e.g., `from pathlib import Path` or `import pathlib as pl`)
    path_classes: set[str] = {"Path"}
    pathlib_modules: set[str] = {"pathlib"}
    path_variables: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pathlib":
                    pathlib_modules.add(alias.asname or alias.name)
                elif alias.name in ("shutil", "os", "io", "_io"):
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pathlib":
                for alias in node.names:
                    if alias.name == "Path":
                        path_classes.add(alias.asname or alias.name)
            elif node.module == "shutil":
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

    def is_path_receiver(expr: ast.AST) -> bool:
        """Check if an AST expression evaluates to a Path object."""
        if isinstance(expr, ast.Name):
            return expr.id in path_variables or expr.id in path_classes
        elif isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Name) and expr.func.id in path_classes:
                return True
            if (
                isinstance(expr.func, ast.Attribute)
                and isinstance(expr.func.value, ast.Name)
                and expr.func.value.id in pathlib_modules
                and expr.func.attr == "Path"
            ):
                return True
            if isinstance(expr.func, ast.Attribute) and is_path_receiver(
                expr.func.value
            ):
                return True
        elif isinstance(expr, ast.BinOp):
            return is_path_receiver(expr.left) or is_path_receiver(expr.right)
        elif isinstance(expr, ast.Attribute):
            return is_path_receiver(expr.value)
        return False

    # First pass: collect variables assigned from Path expressions
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if is_path_receiver(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        path_variables.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value
            and is_path_receiver(node.value)
            and isinstance(node.target, ast.Name)
        ):
            path_variables.add(node.target.id)

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

            # Check `shutil.<func>` or `os.<func>` or `io.open` (including aliased imports)
            if isinstance(func.value, ast.Name):
                module_name = module_aliases.get(func.value.id, func.value.id)
                alias_info = (
                    f" (via '{func.value.id}')" if func.value.id != module_name else ""
                )
                if module_name == "shutil" and attr_name in FORBIDDEN_SHUTIL_FUNCS:
                    violations.append(
                        f"{file_path}:{lineno}: Direct call to 'shutil.{attr_name}()'{alias_info}. All file I/O must go through storage.py"
                    )
                elif module_name == "os" and attr_name in FORBIDDEN_OS_FUNCS:
                    violations.append(
                        f"{file_path}:{lineno}: Direct call to 'os.{attr_name}()'{alias_info}. All file I/O must go through storage.py"
                    )
                elif module_name in ("io", "_io") and attr_name == "open":
                    violations.append(
                        f"{file_path}:{lineno}: Direct call to 'io.open()'{alias_info}. All file I/O must go through storage.py"
                    )

            # Check `Path(...).<method>()` or `p.<method>()`
            if attr_name in FORBIDDEN_PATH_METHODS:
                violations.append(
                    f"{file_path}:{lineno}: Direct call to Path method '.{attr_name}()'. All file I/O must go through storage.py"
                )
            elif attr_name == "open" and is_path_receiver(func.value):
                violations.append(
                    f"{file_path}:{lineno}: Direct call to Path method '.open()'. All file I/O must go through storage.py"
                )

    return violations


def test_app_storage_boundary() -> None:
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


def test_storage_boundary_detects_violations() -> None:
    """
    Regression test: Verify that deliberate direct file I/O operations are detected,
    including aliased imports, os.open, and Path.open calls.
    """
    violating_snippets = """
from pathlib import Path
import pathlib as pl
import os
import shutil
import shutil as sh
import os as my_os
import io as custom_io
from os import remove

def do_forbidden():
    with open("sample.txt", "w") as f:
        f.write("hello")

    shutil.copy("a.mp4", "b.mp4")
    sh.rmtree("/tmp/folder")
    my_os.remove("old.mp4")
    remove("another_old.mp4")
    custom_io.open("test.txt")
    os.open("lowlevel.txt", 0)

    p = Path("video.mp4")
    p.write_bytes(b"123")
    p.read_text()
    p.unlink()
    p.open("wb")

    Path("direct.mp4").open("wb")

    p2 = pl.Path("aliased.mp4")
    p2.open("rb")
"""
    violations = find_storage_violations(violating_snippets, file_path="fixture.py")

    assert any("builtin 'open()'" in v for v in violations), (
        "Should catch builtin open()"
    )
    assert any("shutil.copy()" in v for v in violations), "Should catch shutil.copy()"
    assert any("shutil.rmtree()" in v for v in violations), (
        "Should catch aliased sh.rmtree()"
    )
    assert any("os.remove()" in v for v in violations), (
        "Should catch aliased my_os.remove()"
    )
    assert any("os.open()" in v for v in violations), "Should catch os.open()"
    assert any("io.open()" in v for v in violations), (
        "Should catch aliased custom_io.open()"
    )
    assert any(".write_bytes()" in v for v in violations), (
        "Should catch Path.write_bytes()"
    )
    assert any(".read_text()" in v for v in violations), "Should catch Path.read_text()"
    assert any(".unlink()" in v for v in violations), "Should catch Path.unlink()"
    assert any(".open()" in v for v in violations), "Should catch Path.open()"


def test_storage_boundary_allows_non_path_open_calls() -> None:
    """
    Verify that legitimate .open() calls on non-Path objects (e.g. webbrowser, PIL, zipfile)
    are not falsely flagged as violations.
    """
    valid_snippets = """
import webbrowser
from PIL import Image
import zipfile

def open_unrelated_things():
    webbrowser.open("https://example.com")
    img = Image.open("image.png")
    with zipfile.ZipFile("archive.zip") as zf:
        zf.open("file.txt")
"""
    violations = find_storage_violations(valid_snippets, file_path="valid_fixture.py")
    assert violations == [], (
        f"Expected 0 violations for non-path .open() calls, got: {violations}"
    )


def test_storage_boundary_respects_exemptions() -> None:
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
