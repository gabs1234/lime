"""Compose math and selectable text on the terminal's physical row grid.

Rich lays out lists, quotes and margins using ordinary one-cell placeholders.
Only the final output stream expands those placeholders into quiet Kitty PNG
placements. Each image tile occupies exactly one existing row: no hidden cursor
motion, no alternate screen, and no images extending below unreserved rows.
"""

from __future__ import annotations

import html
import io
import math
import re
from dataclasses import dataclass
from typing import TextIO

from PIL import Image
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import MarkdownElement, Paragraph, TableDataElement
from rich.measure import Measurement
from rich.text import Text

from lime.cache import ImageCache
from lime.graphics import encode_png
from lime.math_render import Expression, MathError, MathImage, MathRenderer
from lime.terminal import Terminal


class GraphicsStream:
    """Expand internal placeholders after Rich has finished measuring cells."""

    def __init__(self, inner: TextIO, registry: MathGraphics) -> None:
        self.inner, self.registry = inner, registry

    def write(self, text: str) -> int:
        self.inner.write(text.translate(self.registry.commands))
        return len(text)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class MathGraphics:
    def __init__(self, terminal: Terminal, source: str = "") -> None:
        self.terminal = terminal
        self.commands: dict[int, str] = {}
        self._tiles: dict[tuple, str] = {}
        self._next = 0xF0000
        self._bytes = 0
        self.max_bytes = 32 * 1024 * 1024
        self._reserved = {ord(c) for c in html.unescape(source) if ord(c) >= self._next}
        self._rasters = ImageCache[Image.Image](max_bytes=64 * 1024 * 1024, max_entries=8)

    def reset(self) -> None:
        self.commands.clear()
        self._tiles.clear()
        self._next, self._bytes = 0xF0000, 0

    def tile(self, image: MathImage, columns: int, top: int, row: int) -> str:
        key = (image, columns, top, row)
        if key in self._tiles:
            return self._tiles[key]
        cw, ch = self.terminal.cell_width * 2, self.terminal.cell_height * 2
        canvas = Image.new("RGBA", (columns * cw, ch))
        # Paste may have a negative origin: Pillow clips to the physical row.
        raster = self._rasters.get(image)
        if raster is None:
            raster = image.image()
            self._rasters.put(image, raster, image.width * image.height * 4)
        canvas.paste(raster, (0, top - row * ch))
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        while self._next in self._reserved:
            self._next += 1
        if self._next > 0xFFFFD:
            raise MathError("too many equation tiles")
        command = "".join(encode_png(buffer.getvalue(), columns, 1)) + " "
        if self._bytes + len(command) > self.max_bytes:
            raise MathError("math placements exceed the 32 MiB budget")
        self._bytes += len(command)
        marker = chr(self._next)
        self._next += 1
        self.commands[ord(marker)] = command
        self._tiles[key] = marker
        return marker


@dataclass(frozen=True)
class Picture:
    image: MathImage
    columns: int
    source: str = "(math)"


def picture(image: MathImage, width: int, terminal: Terminal) -> Picture:
    columns = max(1, math.ceil(image.width / (terminal.cell_width * 2)))
    if columns > width:
        ratio = width * terminal.cell_width * 2 / image.width
        if ratio < 0.8:
            raise MathError("equation is wider than the available space")
        raster = image.image()
        size = (width * terminal.cell_width * 2, max(1, round(image.height * ratio)))
        raster = raster.resize(size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        raster.save(buffer, format="PNG")
        image = MathImage(buffer.getvalue(), *size, image.baseline * ratio)
        columns = width
    return Picture(image, columns)


def compose_line(units: list[Text | Picture], graphics: MathGraphics) -> list[Text]:
    """Place text and all equation baselines on the same physical row baseline."""
    terminal = graphics.terminal
    ch = terminal.cell_height
    # Terminals report cell dimensions but not font ascent. This conventional
    # text baseline is calibrated visually; each equation's baseline is measured.
    text_baseline = round(ch * 0.8)
    pictures = [unit for unit in units if isinstance(unit, Picture)]
    if not pictures:
        return [Text.assemble(*units)]
    ascent = max(text_baseline, *(p.image.baseline / 2 for p in pictures))
    descent = max(ch - text_baseline, *((p.image.height - p.image.baseline) / 2 for p in pictures))
    text_row = max(0, math.ceil((ascent - text_baseline) / ch))
    baseline = text_row * ch + text_baseline
    rows = max(text_row + 1, math.ceil((baseline + descent) / ch))
    result = []
    for row in range(rows):
        line = Text(end="\n", overflow="crop", no_wrap=True)
        for unit in units:
            if isinstance(unit, Text):
                line.append(unit if row == text_row else " " * unit.cell_len)
            else:
                top = round(baseline * 2 - unit.image.baseline)
                line.append(graphics.tile(unit.image, unit.columns, top, row))
                line.append(" " * (unit.columns - 1))
        result.append(line)
    return result


def wrap_parts(
    parts: list[Text | Expression],
    renderer: MathRenderer,
    graphics: MathGraphics,
    console: Console,
    width: int,
) -> list[Text]:
    """Wrap whole equations and styled words; fold long text without losing glyphs."""
    result: list[Text] = []
    units: list[Text | Picture] = []
    used = 0

    def finish() -> None:
        nonlocal units, used
        while units and isinstance(units[-1], Text) and units[-1].plain.isspace():
            units.pop()
        if units:
            try:
                result.extend(compose_line(units, graphics))
            except MathError:
                text = Text.assemble(
                    *(unit if isinstance(unit, Text) else unit.source for unit in units)
                )
                result.extend(text.wrap(console, width, overflow="fold"))
        units, used = [], 0

    def text_piece(text: Text) -> None:
        nonlocal used
        for match in re.finditer(r"\n|[^\S\n]+|[^\s]+", text.plain):
            word = text[match.start() : match.end()]
            if word.plain == "\n":
                finish()
                continue
            if word.plain.isspace():
                if units and used < width:
                    gap = Text(" ", style=word.style)
                    units.append(gap)
                    used += 1
                continue
            if used and used + word.cell_len > width:
                finish()
            # Rich's cell-aware fold preserves wide characters and styled spans.
            folded = word.wrap(console, width, overflow="fold", no_wrap=False)
            for index, fragment in enumerate(folded):
                if index:
                    finish()
                units.append(fragment)
                used += fragment.cell_len

    for part in parts:
        if isinstance(part, Text):
            text_piece(part)
            continue
        try:
            atom = picture(renderer.get(part), width, graphics.terminal)
            atom = Picture(atom.image, atom.columns, part.fallback())
        except (OSError, ValueError):
            text_piece(Text(part.fallback(), style="cyan"))
            continue
        if used and used + atom.columns > width:
            finish()
        units.append(atom)
        used += atom.columns
    finish()
    return result or [Text()]


class MathSpan(MarkdownElement):
    new_line = False

    @classmethod
    def create(cls, markdown, token):
        return cls(token.meta["expression"])

    def __init__(self, expression: Expression) -> None:
        self.expression = expression

    def __rich_console__(self, console, options):
        yield Text(self.expression.fallback(), style="cyan")


@dataclass
class MathContent:
    """The same measured inline layout can live in a paragraph or a table cell."""

    parts: list[Text | Expression]
    renderer: MathRenderer | None
    graphics: MathGraphics | None
    show_source: bool = False
    justify: str = "left"

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        minimum = maximum = line = 0
        for part in self.parts:
            if isinstance(part, Expression):
                try:
                    if self.renderer is None or self.graphics is None:
                        raise MathError("text output")
                    image = self.renderer.get(part)
                    width = math.ceil(image.width / (self.graphics.terminal.cell_width * 2))
                    minimum = max(minimum, width)
                    line += width
                    continue
                except (OSError, ValueError):
                    part = Text(part.fallback())
            for index, fragment in enumerate(part.split("\n", allow_blank=True)):
                if index:
                    maximum, line = max(maximum, line), 0
                measure = fragment.__rich_measure__(console, options)
                minimum = max(minimum, measure.minimum)
                line += measure.maximum
        return Measurement(minimum, max(minimum, maximum, line))

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self.renderer is None or self.graphics is None:
            yield Text.assemble(
                *(part if isinstance(part, Text) else part.fallback() for part in self.parts),
                justify=self.justify,
            )
            return
        for line in wrap_parts(
            self.parts, self.renderer, self.graphics, console, max(1, options.max_width)
        ):
            # Rich right/center justification strips trailing spaces. Those
            # spaces reserve the remaining cells of an image, so align the full
            # physical span ourselves before handing it back as left-aligned text.
            spare = max(0, options.max_width - line.cell_len)
            if self.justify == "right":
                line.pad_left(spare)
            elif self.justify == "center":
                line.pad_left(spare // 2)
            line.justify = "left"
            yield line
        if self.show_source:
            for part in self.parts:
                if isinstance(part, Expression):
                    yield Text(part.fallback(), style="dim")


class MathTableCell(TableDataElement):
    @classmethod
    def create(cls, markdown, token):
        result = super().create(markdown, token)
        result.renderer, result.graphics = markdown.math_renderer, markdown.math_graphics
        result.show_source = markdown.show_math_source
        result.parts = []
        return result

    def on_text(self, context, text):
        value = Text(text, style=context.current_style) if isinstance(text, str) else text.copy()
        self.content.append_text(value)
        if self.parts and isinstance(self.parts[-1], Text):
            self.parts[-1].append(value)
        else:
            self.parts.append(value)

    def on_child_close(self, context, child):
        if isinstance(child, MathSpan):
            self.parts.append(child.expression)
            self.content.append(child.expression.fallback(), style="cyan")
            return False
        return super().on_child_close(context, child)

    @property
    def renderable(self) -> Text | MathContent:
        if self.renderer and self.graphics and any(isinstance(p, Expression) for p in self.parts):
            return MathContent(
                self.parts, self.renderer, self.graphics, self.show_source, self.justify
            )
        return self.content

    def labelled(self, header: MathTableCell) -> MathContent:
        parts = []
        for part in header.parts:
            if isinstance(part, Text):
                part = part.copy()
                part.stylize("bold")
            parts.append(part)
        return MathContent(
            [*parts, Text(": "), *self.parts], self.renderer, self.graphics, self.show_source
        )


class MathParagraph(Paragraph):
    @classmethod
    def create(cls, markdown, token):
        result = cls(justify=markdown.justify or "left")
        result.renderer, result.graphics = markdown.math_renderer, markdown.math_graphics
        result.show_source = markdown.show_math_source
        result.parts = []
        return result

    def on_text(self, context, text):
        super().on_text(context, text)
        value = Text(text, style=context.current_style) if isinstance(text, str) else text.copy()
        if self.parts and isinstance(self.parts[-1], Text):
            self.parts[-1].append(value)
        else:
            self.parts.append(value)

    def on_child_close(self, context, child):
        if isinstance(child, MathSpan):
            self.parts.append(child.expression)
            return False
        return super().on_child_close(context, child)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        expressions = [part for part in self.parts if isinstance(part, Expression)]
        if not expressions:
            yield from super().__rich_console__(console, options)
        elif self.renderer is None or self.graphics is None:
            yield Text.assemble(
                *(part if isinstance(part, Text) else part.fallback() for part in self.parts)
            )
        else:
            yield MathContent(
                self.parts, self.renderer, self.graphics, self.show_source, self.justify
            )


class MathBlock(MarkdownElement):
    @classmethod
    def create(cls, markdown, token):
        result = cls()
        result.expression = token.meta["expression"]
        result.renderer, result.graphics = markdown.math_renderer, markdown.math_graphics
        result.show_source = markdown.show_math_source
        return result

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self.renderer is None or self.graphics is None:
            yield Text(self.expression.fallback(), style="cyan")
            return
        try:
            atom = picture(
                self.renderer.get(self.expression),
                max(1, options.max_width),
                self.graphics.terminal,
            )
            margin = max(0, (options.max_width - atom.columns) // 2)
            # Display equations need no terminal-text baseline alignment.
            rows = max(1, math.ceil(atom.image.height / (self.graphics.terminal.cell_height * 2)))
            lines = []
            for row in range(rows):
                marker = self.graphics.tile(atom.image, atom.columns, 0, row)
                lines.append(Text(" " * margin + marker + " " * (atom.columns - 1), no_wrap=True))
            yield from lines
        except (OSError, ValueError) as error:
            yield Text(f"Math: {error}; showing source", style="dim")
            yield Text(self.expression.fallback(), style="cyan")
        else:
            if self.show_source:
                yield Text(self.expression.fallback(), style="dim")
