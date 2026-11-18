import io
import re
import shutil
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.console import Console
from test_math import FakeBackend, raster, rendered, without_graphics

from lime.math_layout import MathContent, MathGraphics
from lime.math_render import Expression, MathRenderer, TypstBackend
from lime.render import Document, math_expressions
from lime.terminal import Terminal

TABLE = (Path(__file__).parent / "fixtures/math-table.md").read_text()


def test_table_math_remains_math_tokens_including_headers():
    assert math_expressions(Document("| $x$ |\n|---|\n| $y$ |").parsed) == [
        Expression("x"),
        Expression("y"),
    ]
    expressions = math_expressions(Document(TABLE).parsed)
    assert len(expressions) == 4
    assert expressions[0].source == r"LF\to\infty"
    assert all(r"\displaystyle" in item.source for item in expressions[1:])


@pytest.mark.parametrize("width", [12, 20, 32, 48, 88, 120])
def test_table_equations_render_without_losing_text_or_crossing_borders(width):
    result, row, _ = rendered(TABLE, width=width)
    plain = without_graphics(result)
    assert "\x1b_G" in result and r"\frac" not in plain and "$" not in plain
    joined = re.sub(r"[\s│]", "", plain)
    for words in (
        "Assessment",
        "Relative",
        "squared",
        "projection",
        "error",
        "Exact spectrum",
        "Existing rectangular integration-by-parts bound",
        "Proposed general BV bound",
    ):
        assert re.sub(r"\s", "", words) in joined
    assert all(cell_len(line) <= width for line in plain.splitlines())
    assert row == plain.count("\n")
    borders = [
        [i for i, c in enumerate(line) if c == "│"]
        for line in plain.splitlines()
        if line.startswith("│")
    ]
    assert not borders or all(border == borders[0] for border in borders)
    for raw_line in result.splitlines():
        visible = without_graphics(raw_line)
        edges = [i for i, char in enumerate(visible) if char == "│"]
        for packet in re.finditer(r"\x1b_G([^;]*);[^\x1b]*\x1b\\", raw_line):
            columns = re.search(r"(?:^|,)c=(\d+)", packet[1])
            if columns is None:
                continue
            start = cell_len(without_graphics(raw_line[: packet.start()]))
            end = start + int(columns[1])
            if edges:
                right = next(edge for edge in edges if edge > start)
                assert end <= right - 1  # preserve the cell's right padding
            else:
                assert end <= width


def test_column_alignment_is_preserved_for_math():
    source = "| Left | Center | Right |\n|:---|:---:|---:|\n| $x$ | $y$ | $z$ |"
    result, _, _ = rendered(source, width=50, backend=FakeBackend(raster(48, 20, 15)))
    line = next(line for line in result.splitlines() if "\x1b_G" in line)
    positions = [
        cell_len(without_graphics(line[: match.start()])) for match in re.finditer(r"\x1b_G", line)
    ]
    plain = without_graphics(line)
    borders = [i for i, c in enumerate(plain) if c == "│"]
    assert len(positions) == 3 and len(borders) == 4
    assert positions[0] == borders[0] + 2
    center_width = borders[2] - borders[1] - 3
    assert positions[1] == borders[1] + 2 + (center_width - 3) // 2
    assert positions[2] == borders[3] - 4


def test_measuring_math_uses_image_width_instead_of_latex_source_length():
    console = Console(file=io.StringIO(), width=80)
    backend = FakeBackend(raster(48))
    renderer = MathRenderer(17, (220, 220, 220), backend=backend)
    content = MathContent(
        [Expression(r"\displaystyle \frac{4}{\pi^2LF}")], renderer, MathGraphics(Terminal())
    )
    measure = content.__rich_measure__(console, console.options)
    assert measure.minimum == measure.maximum == 3
    assert len(backend.calls) == 1
    content.__rich_measure__(console, console.options)
    assert len(backend.calls) == 1


def test_wide_equations_switch_to_labelled_records_to_use_available_width():
    source = "| Name | Value |\n|---|---:|\n| First | $x$ |\n| Second | $y$ |"
    result, _, _ = rendered(source, width=30, backend=FakeBackend(raster(320, 20, 15)))
    plain = without_graphics(result)
    assert "╭" not in plain and "Name: First" in plain and "Name: Second" in plain
    assert "\x1b_G" in result and "$x$" not in plain and "$y$" not in plain


@pytest.mark.parametrize("graphics", [True, False])
def test_missing_renderer_or_invalid_math_retains_source(graphics):
    result, _, _ = rendered("| Name | Value |\n|---|---:|\n| Visible | $bad$ |", graphics=graphics)
    plain = without_graphics(result)
    assert "Visible" in plain and "$bad$" in plain


def test_table_source_option_and_code_spans_remain_readable():
    source = "| $x$ | Code |\n|---|---|\n| $y$ | `$z$` |"
    result, _, _ = rendered(source, width=60, labels=True)
    plain = without_graphics(result)
    assert "$x$" in plain and "$y$" in plain and "$z$" in plain
    assert result.count("\x1b_G") == 4


@pytest.mark.parametrize("prefix", ["> ", "- "])
def test_tables_keep_their_container_prefixes(prefix):
    lines = TABLE.splitlines()
    if prefix == "> ":
        source = "\n".join("> " + line for line in lines)
    else:
        source = "- Item\n\n" + "\n".join("  " + line for line in lines)
    result, _, _ = rendered(source, width=80)
    assert "\x1b_G" in result and r"\frac" not in without_graphics(result)
    assert "▌" in result if prefix == "> " else "Item" in result


@pytest.mark.skipif(shutil.which("typst") is None, reason="optional Typst CLI is not installed")
def test_real_projection_error_table_renders_all_four_expressions():
    result, _, _ = rendered(TABLE, width=88, backend=TypstBackend())
    plain = without_graphics(result)
    assert "\x1b_G" in result and "$" not in plain and r"\frac" not in plain
    assert "╭" in plain and "╯" in plain
