import base64
import io
import json
import re
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import pytest
from PIL import Image
from rich.cells import cell_len
from rich.console import Console

from lime.cache import ImageCache
from lime.math_layout import MathGraphics, Picture, compose_line
from lime.math_render import (
    Expression,
    MathError,
    MathImage,
    MathRenderer,
    TypstBackend,
    equation_job,
    validate,
)
from lime.render import Document, RowCounter, math_expressions, render
from lime.terminal import Terminal

CORPUS = json.loads((Path(__file__).parent / "fixtures/math-corpus.json").read_text())


def raster(width=36, height=36, baseline=27):
    output = io.BytesIO()
    Image.new("RGBA", (width, height), (220, 220, 220, 255)).save(output, format="PNG")
    return MathImage(output.getvalue(), width, height, baseline)


class FakeBackend:
    command = "fake-typst"
    identity = ("fake-typst", "1")

    def __init__(self, image=None):
        self.calls = []
        self.image = image or raster()

    def render(self, expressions, size, color):
        self.calls.append((expressions, size, color))
        for expression in expressions:
            validate(expression)
            if expression.source == "bad":
                raise MathError("unknown command")
        return [self.image for expression in expressions]


def rendered(source, width=40, *, backend=None, graphics=True, labels=False, margin=0):
    output = io.StringIO()
    counter = RowCounter(output)
    terminal = Terminal(columns=width, rows=10)
    renderer = MathRenderer(terminal.cell_height, (220, 220, 220), backend=backend or FakeBackend())
    anchors = []
    render(
        source,
        Console(file=counter, width=width),
        base=Path.cwd(),
        math_renderer=renderer if graphics else None,
        math_terminal=terminal if graphics else None,
        show_math_source=labels,
        anchors=anchors,
        margin=margin,
    )
    return output.getvalue(), counter.row, anchors


def without_graphics(source):
    return re.sub(r"\x1b_G[^\x1b]*\x1b\\", "", source)


def tokens(source):
    return math_expressions(Document(source).parsed)


@pytest.mark.parametrize(
    "source,display",
    [
        ("$x^2$", False),
        (r"\(x^2\)", False),
        ("$$x^2$$", True),
        (r"\[x^2\]", True),
        ("$$\nx^2\n$$", True),
        ("> $x^2$", False),
        ("- $x^2$", False),
    ],
)
def test_parser_recognizes_math_in_markdown_contexts(source, display):
    expressions = tokens(source)
    assert len(expressions) == 1 and expressions[0].display is display
    assert expressions[0].source.strip() == "x^2"


@pytest.mark.parametrize(
    "source",
    [
        r"Pay $5 and $10.",
        r"Escaped \$x\$",
        r"\\(x\\)",
        "`$x$`",
        "```python\n$x$\n```",
        "    $x$",
        "An unclosed $x",
        "# Heading $x$",
        "[a $x$](https://example.com)",
    ],
)
def test_math_is_not_taken_from_code_currency_or_unsupported_contexts(source):
    assert not tokens(source)


@pytest.mark.parametrize(
    "label,language",
    [
        ("math", "latex"),
        ("latex-math", "latex"),
        ("typst-math", "typst"),
    ],
)
def test_fences_select_a_math_language(label, language):
    assert tokens(f"```{label}\nx^2\n```") == [Expression("x^2", language, True)]


@pytest.mark.parametrize("prefix", ["> ", "  "])
def test_multiline_math_uses_adjusted_container_offsets(prefix):
    document = (
        ("- item\n" if prefix == "  " else "") + prefix + "$$\n" + prefix + "x^2\n" + prefix + "$$"
    )
    assert tokens(document)[0].source.strip() == "x^2"


@pytest.mark.parametrize("opener", ["$$x$$ trailing prose", "$$x", "\\[x"])
def test_malformed_display_math_does_not_consume_the_next_heading_or_equation(opener):
    source = f"{opener}\n# Heading\n$$y$$"
    document = Document(source)
    assert any(token.type == "heading_open" for token in document.parsed)
    assert math_expressions(document.parsed) == [Expression("y", display=True)]


@pytest.mark.parametrize(
    "source",
    [
        r"[\(x](https://example.com) followed by \)",
        "[a $x](https://example.com) followed by $",
    ],
)
def test_inline_math_does_not_consume_a_markdown_link_boundary(source):
    document = Document(source)
    children = document.parsed[1].children
    assert children[0].type == "link_open"
    assert children[0].attrGet("href") == "https://example.com"
    assert not math_expressions(document.parsed)


def test_complete_math_example_renders_with_and_without_graphics():
    source = (Path(__file__).parents[1] / "examples/math.md").read_text()
    for graphics in (True, False):
        result, _, _ = rendered(source, graphics=graphics)
        assert "Source and fallback" in result and "Energy" in result
        assert "std" not in result


@pytest.mark.parametrize("source", [r"x\label{eq}", r"\ref{eq}", r"\iftypst x"])
def test_mitex_commands_that_could_drop_information_are_refused(source):
    with pytest.raises(MathError):
        validate(Expression(source))


@pytest.mark.parametrize("source,starred", [(r"x=y\tag{1}", False), (r"x=y\tag*{S1}", True)])
def test_display_tags_are_preserved_and_starred_tags_are_normalized(source, starred):
    expression = Expression(source, display=True)
    validate(expression)
    job = equation_job(expression)
    assert job["tag_starred"] is starred
    assert job["source"] == source.replace(r"\tag*", r"\tag")
    assert expression.source == source


def test_equation_tags_need_display_mode_and_multiple_tags_do_not_get_silently_merged():
    with pytest.raises(MathError, match="display math"):
        validate(Expression(r"x=y\tag{1}"))
    with pytest.raises(MathError, match="multiple equation tags"):
        validate(Expression(r"x=y\tag{1}\\y=z\tag{2}", display=True))


def test_commented_or_escaped_tag_commands_do_not_change_equation_jobs():
    expression = Expression("x=y % \\tag*{ignored}\n", display=True)
    validate(expression)
    job = equation_job(expression)
    assert job["source"] == expression.source and not job["tag_starred"]
    validate(Expression(r"x\text{\\tag{literal}}"))


@pytest.mark.parametrize(
    "source",
    [
        '#read("secret")',
        'std.read("secret")',
        '#import "@preview/foo:1.0.0"',
        "$x$",
        r'\" #text("outside the quote")',
        'x // comment\u2028#text("outside the comment")',
        'x /* nested /* comment */ */ #text("outside the comment")',
    ],
)
def test_typst_snippets_cannot_enter_code_or_load_packages(source):
    with pytest.raises(MathError):
        validate(Expression(source, "typst"))


def test_typst_quoted_text_may_contain_special_characters():
    source = Expression(r'x "std costs $5 # today" \# \$', "typst")
    assert equation_job(source)["source"] == r'x "std costs \u{24}5 \u{23} today" \u{23} \u{24}'


@pytest.mark.parametrize("command", [r"\hspace{2pt}", r"\vspace{2pt}", r"\raisebox{2pt}{x}"])
def test_latex_commands_that_evaluate_code_leave_source_visible(command):
    with pytest.raises(MathError, match="unsupported equation commands"):
        equation_job(Expression(command))


def test_source_budget_is_checked_before_spawning_a_renderer():
    with pytest.raises(MathError, match="32 KiB"):
        validate(Expression("x" * 32769))


def test_successful_equations_are_batched_and_bad_equations_are_isolated():
    backend = FakeBackend()
    renderer = MathRenderer(17, (255, 255, 255), backend=backend)
    good, bad = Expression("x"), Expression("bad")
    renderer.prepare([good, good, bad, Expression("y")])
    assert len(backend.calls[0][0]) == 3
    assert renderer.get(good).width > 0
    assert renderer.get(Expression("y")).width > 0
    with pytest.raises(MathError, match="unknown command"):
        renderer.get(bad)
    before = len(backend.calls)
    renderer.prepare([good, bad])
    assert len(backend.calls) == before


def test_math_cache_tracks_raster_inputs_and_retries_failures_on_new_reprint():
    cache = ImageCache()
    backend = FakeBackend()
    equation = Expression("x")
    for height, color in [
        (17, (255, 255, 255)),
        (17, (255, 255, 255)),
        (20, (255, 255, 255)),
        (20, (0, 0, 0)),
    ]:
        MathRenderer(height, color, backend=backend, cache=cache).get(equation)
    assert len(backend.calls) == 3
    MathRenderer(20, (0, 0, 0), backend=backend, cache=cache).get(Expression("x", display=True))
    assert len(backend.calls) == 4
    first = MathRenderer(17, (255, 255, 255), backend=backend, cache=cache)
    with pytest.raises(MathError):
        first.get(Expression("bad"))
    second = MathRenderer(17, (255, 255, 255), backend=backend, cache=cache)
    before = len(backend.calls)
    with pytest.raises(MathError):
        second.get(Expression("bad"))
    assert len(backend.calls) == before + 1


def test_long_documents_prefetch_in_order_without_thrashing_the_bounded_cache():
    backend = FakeBackend()
    cache = ImageCache(max_entries=40)
    renderer = MathRenderer(17, (220, 220, 220), backend=backend, cache=cache)
    expressions = [Expression(f"x_{index}") for index in range(300)]
    renderer.schedule(expressions)
    for expression in expressions:
        assert renderer.get(expression).height > 0
    assert len(backend.calls) == 10
    assert sum(len(call[0]) for call in backend.calls) == len(expressions)


def test_source_remains_readable_without_graphics():
    result, _, _ = rendered("Before $x^2$ after.\n\n$$\\frac{a}{b}$$", graphics=False)
    assert "Before $x^2$ after." in result and r"\frac{a}{b}" in result
    assert "\x1b" not in result


@pytest.mark.parametrize("container", ["{}", "> {}", "- {}", "1. {}", "> - {}"])
@pytest.mark.parametrize("width", [12, 20, 40, 80])
def test_inline_math_wraps_in_containers_and_preserves_selectable_text(container, width):
    source = container.format("Before **bold** $x^2$ after 日本語.")
    result, row, _ = rendered(source, width=width)
    assert "\x1b_G" in result
    assert not any(0xF0000 <= ord(c) <= 0xFFFFD for c in result)
    text = without_graphics(result)
    assert all(cell_len(line) <= width for line in text.splitlines())
    joined = re.sub(r"[\s▌•]", "", text)
    assert all(word in joined for word in ("Before", "bold", "after", "日本語"))
    assert row == result.count("\n")


def test_math_images_use_complete_physical_cells_and_quiet_packets():
    result, _, _ = rendered("Text $x$ end.", backend=FakeBackend(raster(35, 65, 43)))
    packets = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", result)
    assert len(packets) > 1
    for control, payload in packets:
        assert "r=1" in control and "C=1" in control and "q=2" in control
        image = Image.open(io.BytesIO(base64.b64decode(payload)))
        columns = int(re.search(r"c=(\d+)", control)[1])
        assert image.size == (columns * 16, 34)


def test_equations_share_a_measured_baseline_in_the_same_line():
    from rich.text import Text

    graphics = MathGraphics(Terminal())
    line = compose_line(
        [Text("a"), Picture(raster(20, 60, 45), 2), Text("b"), Picture(raster(20, 24, 18), 2)],
        graphics,
    )
    assert len(line) >= 2
    text_rows = [i for i, row in enumerate(line) if "a" in row.plain]
    assert len(text_rows) == 1 and "b" in line[text_rows[0]].plain
    # Both equations' baselines are aligned with this text row's baseline.
    expected = (text_rows[0] * 17 + round(17 * 0.8)) * 2
    tiles = list(graphics._tiles)
    for image, columns, top, row in tiles:
        assert abs(top + image.baseline - expected) <= 0.5


def test_placement_budget_keeps_inline_source_readable():
    from rich.text import Text

    from lime.math_layout import wrap_parts

    graphics = MathGraphics(Terminal())
    graphics.max_bytes = 1
    renderer = MathRenderer(17, (220, 220, 220), backend=FakeBackend())
    lines = wrap_parts(
        [Text("Before "), Expression("x"), Text(" after")],
        renderer,
        graphics,
        Console(file=io.StringIO()),
        40,
    )
    result = "".join(line.plain for line in lines)
    assert "Before $x$ after" == result
    assert not graphics.commands


def test_user_private_characters_and_decoded_entities_do_not_become_image_commands():
    result, _, _ = rendered("&#983040; $x$")
    assert chr(0xF0000) in result
    assert "\x1b_G" in result


def test_too_wide_equations_show_source_instead_of_becoming_unreadable():
    result, _, _ = rendered("Before $x$ after.", width=20, backend=FakeBackend(raster(1000)))
    assert "\x1b_G" not in result and "$x$" in result


def test_math_source_is_opt_in_and_heading_anchors_survive_reflow():
    source = "# One\n\nText $x$ end.\n\n$$y$$\n\n## Two"
    first, row, anchors = rendered(source, labels=True, margin=2)
    assert "$x$" in first and "$$" in first and len(anchors) == 3
    second, new_row, new_anchors = rendered(source, labels=True, margin=2)
    assert first == second and row == new_row and anchors == new_anchors
    assert all(a <= b for a, b in pairwise(anchors))


def test_missing_typst_is_a_source_fallback(monkeypatch):
    monkeypatch.setattr("lime.math_render.shutil.which", lambda _: None)
    backend = TypstBackend()
    result, _, _ = rendered("$$x$$", backend=backend)
    assert "install Typst" in result and "$$" in result and "\x1b_G" not in result


def test_timeout_is_bounded_and_diagnostic_is_readable(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] <= 10 and kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs.get("shell", False) is False
        assert "preexec_fn" not in kwargs
        raise subprocess.TimeoutExpired("typst", 10)

    monkeypatch.setattr("lime.math_render.subprocess.run", timeout)
    with pytest.raises(MathError, match="timed out"):
        TypstBackend("fake-typst")._run(["query"], tmp_path)


def test_snap_jobs_use_the_apps_shared_directory_instead_of_private_tmp(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    snap.write_text("launcher")
    command = tmp_path / "bin/typst"
    command.parent.mkdir()
    command.symlink_to(snap)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    parent = TypstBackend(str(command))._temporary_parent()
    assert parent == tmp_path / "user/snap/typst/common" and parent.is_dir()
    assert TypstBackend("/usr/bin/typst")._temporary_parent() is None


def test_oversized_metadata_cannot_trigger_rasterization(monkeypatch):
    calls = []

    def run(self, args, directory):
        calls.append(args)
        return json.dumps(
            [{"lime": True, "width": 100000, "height": 100000, "baseline": 12}]
        ).encode()

    monkeypatch.setattr(TypstBackend, "_run", run)
    with pytest.raises(MathError, match="budget"):
        TypstBackend("fake-typst").render([Expression("x")], 13, (255, 255, 255))
    assert len(calls) == 1 and calls[0][0] == "eval"


@pytest.mark.skipif(shutil.which("typst") is None, reason="optional Typst CLI is not installed")
def test_real_typst_mitex_scientific_corpus_and_baselines():
    backend = TypstBackend()
    expressions = [
        Expression(item[key], language, True)
        for item in CORPUS
        for key, language in (("latex", "latex"), ("typst", "typst"))
    ]
    images = backend.render(expressions, 13, (220, 220, 220))
    assert len(images) == 20
    for image in images:
        assert 0 < image.baseline <= image.height
        raster = image.image()
        assert raster.getbbox() and raster.getextrema()[-1][0] == 0
        assert raster.getpixel((0, 0))[-1] == 0
    # These independently written native forms have identical layout, including
    # the matrix/cases geometry and expanded user macro, not merely a success exit.
    for index in [0, 1, 5, 6, 7]:
        assert images[index * 2] == images[index * 2 + 1]


@pytest.mark.skipif(shutil.which("typst") is None, reason="optional Typst CLI is not installed")
def test_real_unknown_command_fails_and_a_mixed_batch_keeps_good_equations():
    backend = TypstBackend()
    renderer = MathRenderer(17, (220, 220, 220), backend=backend)
    good = Expression(r"\frac{x}{y}")
    bad = Expression(r"\thisCommandDoesNotExist{x}")
    renderer.prepare([good, bad])
    assert renderer.get(good).height > 0
    with pytest.raises(MathError, match="unknown command"):
        renderer.get(bad)


@pytest.mark.skipif(shutil.which("typst") is None, reason="optional Typst CLI is not installed")
def test_real_equation_tags_retain_their_glyphs_and_parentheses():
    images = TypstBackend().render(
        [
            Expression("x=y", display=True),
            Expression(r"x=y\tag{1}", display=True),
            Expression(r"x=y\tag*{1}", display=True),
            Expression(r"x=y\tag{\text{S1}}", display=True),
        ],
        13,
        (220, 220, 220),
    )
    assert images[1].width > images[2].width > images[0].width
    assert images[3].width > images[1].width
    for image in images[1:]:
        # A label must add actual visible pixels at the right, not just whitespace.
        right = image.image().crop((images[0].width, 0, image.width, image.height))
        assert right.getbbox() is not None
        assert abs(image.baseline - images[0].baseline) < 0.01
