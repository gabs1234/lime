from __future__ import annotations

import argparse
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path

from markdown_it import MarkdownIt
from rich.console import Console

from lime.cache import ImageCache
from lime.config import load_settings
from lime.graphics import HeadingImage, Headings
from lime.images import Images
from lime.math_render import Expression, MathImage, MathRenderer
from lime.mermaid import Mermaid
from lime.reader import Layout
from lime.reader import run as read
from lime.render import RowCounter, markdown_theme, render
from lime.terminal import Terminal, clean_text, query_palette, refresh_terminal


def arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lime",
        description="Read Markdown and math in Ghostty.",
        epilog="For a Markdown document containing LaTeX equations, use: lime paper.md",
    )
    parser.add_argument("document", nargs="?", default="-", help="document, or - for stdin")
    parser.add_argument(
        "--format",
        choices=("markdown", "latex-math", "typst-math"),
        default="markdown",
        help="markdown document (default), or a single LaTeX/Typst expression",
    )
    parser.add_argument(
        "--show-math-source",
        action="store_true",
        help="print searchable equation source beneath rendered math",
    )
    parser.add_argument(
        "--once", action="store_true", help="print and exit without clearing scrollback"
    )
    return parser.parse_args(argv)


def equation_source(source: str) -> str:
    """Clipboard snippets may be bare or enclosed in familiar math delimiters."""
    source = source.strip()
    for opening, closing in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
        if (
            source.startswith(opening)
            and source.endswith(closing)
            and len(source) >= 2 * len(opening)
        ):
            return source[len(opening) : -len(closing)].strip()
    return source


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv and sys.stdin.isatty():
        print("usage: lime DOC.md  (or pipe a document to lime)", file=sys.stderr)
        return 2
    try:
        args = arguments(argv)
    except SystemExit as error:
        return int(error.code)
    file = args.document
    try:
        settings = load_settings()
        path = Path(file).expanduser()
        source = sys.stdin.read() if file == "-" else path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"lime: {clean_text(str(error))}", file=sys.stderr)
        return 1

    if args.format != "markdown" and any(
        token.type == "heading_open" for token in MarkdownIt("commonmark").parse(source)
    ):
        suggestion = clean_text(shlex.join(["lime", file]))
        print(
            f"lime: --format {args.format} reads a single equation; this input contains "
            f"Markdown headings. To render the document and its math, use: {suggestion}",
            file=sys.stderr,
        )
        return 2

    terminal = Terminal.detect(sys.stdout)
    plain = "NO_COLOR" in os.environ or not terminal.is_tty
    if not plain:
        terminal = refresh_terminal(terminal, probe=not terminal.graphics)
    content_width = settings.width
    graphics = not plain and terminal.rows >= 6 and terminal.graphics
    native_theme = query_palette() if graphics else None
    normalized_source = clean_text(source)
    heading_cache = ImageCache[tuple[HeadingImage, ...]]()
    mermaid_cache = ImageCache[bytes]()
    math_cache = ImageCache[MathImage]()
    expression = (
        Expression(equation_source(normalized_source), args.format.removesuffix("-math"), True)
        if args.format != "markdown"
        else None
    )
    printed = False

    def print_document(columns: int, rows: int) -> Layout:
        """Render the document once at the given size; report its heading rows.

        The document is a fixed measure centred in the window, so a wider window
        adds an even margin rather than stretching the text. `Layout.anchors`
        holds the output row of the document start and each top-level heading,
        which the reader scrolls to after a reflow.
        """
        nonlocal printed, native_theme
        local = replace(terminal, columns=columns, rows=rows)
        if printed and graphics:
            local = refresh_terminal(local)
            native_theme = query_palette() or native_theme
        printed = True
        vertical = min(settings.vertical_padding, max(0, (rows - 8) // 2)) if terminal.is_tty else 0
        margin = max(0, (columns - min(content_width, columns)) // 2) if terminal.is_tty else 0
        width = (
            max(4, min(content_width + margin * 2, columns)) if terminal.is_tty else content_width
        )
        counter = RowCounter(sys.stdout)
        console = Console(
            file=counter,
            width=width,
            force_terminal=terminal.is_tty and not plain,
            color_system=None if plain else "truecolor",
            no_color=plain,
            theme=markdown_theme(),
            highlight=False,
            markup=False,
        )
        headings = (
            Headings(local, width - margin * 2, native_theme.palette, cache=heading_cache)
            if native_theme
            else None
        )
        mermaid = (
            Mermaid(local, width - margin * 2, native_theme, cache=mermaid_cache)
            if native_theme
            else None
        )
        math_renderer = (
            MathRenderer(local.cell_height, native_theme.foreground, cache=math_cache)
            if native_theme
            else None
        )
        anchors: list[int] = []
        render_options = {
            "base": Path.cwd() if file == "-" else path.resolve().parent,
            "margin": margin,
            "headings": headings,
            "heading_labels": True,
            "line_numbers": True,
            "anchors": anchors,
            "mermaid": mermaid,
            "images": Images(local, width - margin * 2) if graphics else None,
            "math_renderer": math_renderer,
            "math_terminal": local if math_renderer else None,
            "show_math_source": args.show_math_source,
            "expression": expression,
        }
        if normalized_source.strip() and vertical:
            console.print("\n" * vertical, end="")
        sections = render(source, console, **render_options)
        if normalized_source.strip() and vertical:
            console.print("\n" * vertical, end="")
        return Layout(sections, anchors)

    interactive = (
        terminal.graphics
        and normalized_source.strip()
        and not plain
        and terminal.is_tty
        and args.format == "markdown"
        and not args.once
    )
    try:
        if interactive:
            # The reader navigates by absolute scrollback row, so the document
            # has to begin at row 0: clear the screen and scrollback, then print
            # once. (A resize reflow does the same before every reprint.)
            sys.stdout.write("\x1b[2J\x1b[3J\x1b[H")
            sys.stdout.flush()
        layout = print_document(terminal.columns, terminal.rows)
        if interactive:
            return read(
                sys.stdout,
                layout,
                path.name if file != "-" else "stdin",
                terminal,
                reprint=print_document,
            )
    except BrokenPipeError:
        # Avoid a second error from Python's final stdout flush after `| head`.
        null_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_fd, sys.stdout.fileno())
        os.close(null_fd)
        return 0
    except KeyboardInterrupt:
        return 130
    return 0
