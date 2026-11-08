"""Regression tests for theme CSS variables (Issue #364)."""

import re
from pathlib import Path


def _parse_theme_blocks(
    theme_css_content: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Extract CSS variables declared in light and dark theme blocks."""
    uncommented_css = re.sub(r"/\*.*?\*/", "", theme_css_content, flags=re.DOTALL)

    light_match = re.search(
        r"(?::root|html\[data-theme=[\"']light[\"']\])[^{]*\{(.*?)\}",
        uncommented_css,
        re.DOTALL,
    )
    dark_match = re.search(
        r"html\[data-theme=[\"']dark[\"']\][^{]*\{(.*?)\}",
        uncommented_css,
        re.DOTALL,
    )

    assert light_match is not None, "Light theme block not found in theme.css"
    assert dark_match is not None, "Dark theme block not found in theme.css"

    var_pattern = re.compile(r"(--v-[\w-]+)\s*:\s*([^;]+);")

    light_vars = dict(var_pattern.findall(light_match.group(1)))
    dark_vars = dict(var_pattern.findall(dark_match.group(1)))

    return light_vars, dark_vars


def test_v_bg_card_declared_in_light_and_dark_themes() -> None:
    """Verify that --v-bg-card is declared in both light and dark themes."""
    theme_file = (
        Path(__file__).parent.parent / "app" / "ui" / "static" / "css" / "theme.css"
    )
    assert theme_file.is_file(), f"theme.css not found at {theme_file}"

    theme_content = theme_file.read_text(encoding="utf-8")
    light_vars, dark_vars = _parse_theme_blocks(theme_content)

    assert "--v-bg-card" in light_vars, (
        "Missing --v-bg-card declaration in light theme (:root / html[data-theme='light'])"
    )
    assert "--v-bg-card" in dark_vars, (
        "Missing --v-bg-card declaration in dark theme (html[data-theme='dark'])"
    )

    assert "var(--v-bg-surface)" in light_vars["--v-bg-card"]
    assert "var(--v-bg-surface)" in dark_vars["--v-bg-card"]

    assert "--v-bg-surface" in light_vars
    assert "--v-bg-surface" in dark_vars


def test_v_bg_card_usages_have_declarations() -> None:
    """Verify that all application CSS files referencing --v-bg-card have declarations."""
    static_css_dir = Path(__file__).parent.parent / "app" / "ui" / "static" / "css"
    theme_file = static_css_dir / "theme.css"
    theme_content = theme_file.read_text(encoding="utf-8")
    light_vars, dark_vars = _parse_theme_blocks(theme_content)

    css_files = list(static_css_dir.glob("*.css"))
    assert css_files, "No CSS files found"

    card_usages = 0
    for css_file in css_files:
        if css_file.name == "theme.css":
            continue
        content = css_file.read_text(encoding="utf-8")
        matches = re.findall(r"var\((--v-bg-card)\)", content)
        if matches:
            card_usages += len(matches)
            for var_name in matches:
                assert var_name in light_vars, (
                    f"{var_name} referenced in {css_file.name} is missing from light theme"
                )
                assert var_name in dark_vars, (
                    f"{var_name} referenced in {css_file.name} is missing from dark theme"
                )

    assert card_usages >= 6, (
        f"Expected at least 6 usages of --v-bg-card, found {card_usages}"
    )


def test_commented_declaration_is_ignored() -> None:
    """Verify that custom property declarations inside comments are ignored."""
    css_with_light_commented = """
    :root, html[data-theme="light"] {
        /* --v-bg-card: var(--v-bg-surface); */
        --v-bg-surface: #ffffff;
    }
    html[data-theme="dark"] {
        --v-bg-surface: #151922;
        --v-bg-card: var(--v-bg-surface);
    }
    """
    light_vars, dark_vars = _parse_theme_blocks(css_with_light_commented)
    assert "--v-bg-card" not in light_vars
    assert "--v-bg-card" in dark_vars

    css_with_dark_commented = """
    :root, html[data-theme="light"] {
        --v-bg-surface: #ffffff;
        --v-bg-card: var(--v-bg-surface);
    }
    html[data-theme="dark"] {
        --v-bg-surface: #151922;
        /* --v-bg-card: var(--v-bg-surface); */
    }
    """
    light_vars2, dark_vars2 = _parse_theme_blocks(css_with_dark_commented)
    assert "--v-bg-card" in light_vars2
    assert "--v-bg-card" not in dark_vars2
