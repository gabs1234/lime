import io

import pytest

from lime.cli import equation_source, main
from lime.terminal import Terminal, TerminalTheme


@pytest.mark.parametrize("source", ["x^2", "$x^2$", "$$x^2$$", r"\(x^2\)", r"\[x^2\]"])
def test_clipboard_delimiters_are_optional(source):
    assert equation_source(source) == "x^2"


@pytest.mark.parametrize(
    "language,source", [("latex-math", r"\frac{x}{y}"), ("typst-math", "frac(x, y)")]
)
def test_expression_stdin_has_clean_source_when_output_is_redirected(monkeypatch, language, source):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setattr("sys.stdin", io.StringIO(source))
    assert main(["--format", language, "-"]) == 0
    assert source in stream.getvalue() and "\x1b" not in stream.getvalue()


def test_piped_markdown_needs_no_document_argument(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setattr("sys.stdin", io.StringIO("Hello **world**."))
    assert main([]) == 0
    assert "Hello world." in stream.getvalue()


def test_invalid_format_is_a_usage_error(capsys):
    assert main(["--format", "wrong", "-"]) == 2
    assert "invalid choice" in capsys.readouterr().err


def test_help_describes_math_and_stdin(capsys):
    assert main(["--help"]) == 0
    assert "typst-math" in capsys.readouterr().out


def test_markdown_paper_in_expression_mode_explains_the_correct_command(tmp_path, capsys):
    paper = tmp_path / "Chen et al. - diffraction.md"
    paper.write_text("# Paper\n\nA paragraph.\n\n$$x=y\\tag{1}$$")
    assert main(["--format", "latex-math", str(paper)]) == 2
    captured = capsys.readouterr()
    assert "single equation" in captured.err and "Markdown headings" in captured.err
    assert f"lime '{paper}'" in captured.err
    assert not captured.out


def test_markdown_default_keeps_tagged_equations_and_prose(tmp_path, capsys):
    paper = tmp_path / "paper.md"
    paper.write_text("# Paper\n\nReadable prose.\n\n$$x=y\\tag{1}$$")
    assert main([str(paper)]) == 0
    result = capsys.readouterr().out
    assert "Paper" in result and "Readable prose." in result and r"\tag{1}" in result


def test_expression_mode_accepts_an_equation_stored_with_a_markdown_extension(tmp_path, capsys):
    paper = tmp_path / "equation.md"
    paper.write_text(r"$$x=y\tag{1}$$")
    assert main(["--format", "latex-math", str(paper)]) == 0
    assert r"x=y\tag{1}" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [["--format", "latex-math", "-"], ["--once", "-"]])
def test_expression_and_once_modes_preserve_scrollback_and_exit(monkeypatch, arguments):
    stream = io.StringIO()
    called = []
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setattr("sys.stdin", io.StringIO("$x$"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("lime.cli.read", lambda *a, **k: called.append(True))
    monkeypatch.setattr("lime.cli.query_palette", lambda: None)
    monkeypatch.setattr("lime.cli.refresh_terminal", lambda terminal, **kwargs: terminal)
    monkeypatch.setattr(Terminal, "detect", lambda _: Terminal(is_tty=True, graphics=True))
    assert main(arguments) == 0
    assert not called and "\x1b[3J" not in stream.getvalue()


def test_font_zoom_requeries_cells_and_invalidates_raster_cache(monkeypatch):
    from test_math import FakeBackend

    from lime.math_render import MathRenderer

    stream = io.StringIO()
    backend = FakeBackend()
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setattr("sys.stdin", io.StringIO("$x$"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(Terminal, "detect", lambda _: Terminal(is_tty=True, graphics=True))
    theme = TerminalTheme(((10, 20, 30),) * 3, (220, 220, 220), (10, 10, 10))
    monkeypatch.setattr("lime.cli.query_palette", lambda: theme)
    cells = []

    def refresh(terminal, **kwargs):
        from dataclasses import replace

        cells.append(True)
        return replace(
            terminal,
            cell_width=8 if len(cells) == 1 else 10,
            cell_height=17 if len(cells) == 1 else 21,
        )

    monkeypatch.setattr("lime.cli.refresh_terminal", refresh)
    monkeypatch.setattr(
        "lime.cli.MathRenderer",
        lambda height, color, **kwargs: MathRenderer(height, color, backend=backend, **kwargs),
    )

    def reader(*args, reprint):
        reprint(90, 30)
        reprint(90, 40)
        return 0

    monkeypatch.setattr("lime.cli.read", reader)
    assert main(["-"]) == 0
    assert len(backend.calls) == 2
    assert backend.calls[0][1] == 17 * 0.85 and backend.calls[1][1] == 21 * 0.85
