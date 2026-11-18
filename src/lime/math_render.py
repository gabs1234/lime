"""Optional, offline Typst/MiTeX typesetting, independent of terminal placement."""

from __future__ import annotations

import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from PIL import Image

from lime.cache import ImageCache
from lime.terminal import RGB, clean_text

MAX_SOURCE = 32_768
MAX_PIXELS = 8_000_000
MAX_DIMENSION = 16_384
MAX_BATCH = 32
TIMEOUT = 10
SESSION_SECONDS = 20
VENDOR = Path(__file__).parent / "_vendor" / "mitex"
LATEX_COMMANDS = re.compile(r"\\(?:[A-Za-z]+|[^\n])|%[^\n]*")
TYPST_STRING = re.compile(r'"(?:[^"\\]|\\[\s\S])*"')
TYPST_LINE_COMMENT = re.compile(r"//[^\n\r\v\f\x85\u2028\u2029]*")
TYPST_COMMENT_MARKER = re.compile(r"/\*|\*/")
TYPST_STD = re.compile(r"(?<![^\W_])std(?![^\W_])")
MATH_LITERAL = {"#": r"\u{23}", "$": r"\u{24}"}
MATH_LITERAL_TRANSLATION = str.maketrans(MATH_LITERAL)


def latex_commands(source: str) -> list[re.Match[str]]:
    """Skip escaped backslashes and comments when finding active commands."""
    return [match for match in LATEX_COMMANDS.finditer(source) if match[0].startswith("\\")]


def latex_tags(source: str) -> list[re.Match[str]]:
    return [match for match in latex_commands(source) if match[0] == r"\tag"]


class MathError(ValueError):
    """An equation cannot be rendered; its source must remain readable."""


@dataclass(frozen=True)
class Expression:
    source: str
    language: str = "latex"
    display: bool = False

    def fallback(self) -> str:
        if self.language == "typst":
            return f"```typst-math\n{self.source}\n```" if self.display else self.source
        return f"$$\n{self.source}\n$$" if self.display else f"${self.source}$"


@dataclass(frozen=True)
class MathImage:
    png: bytes
    width: int
    height: int
    baseline: float

    def image(self) -> Image.Image:
        with Image.open(io.BytesIO(self.png)) as image:
            return image.convert("RGBA")


# The wrapper reads source as JSON data. It never interpolates it into Typst code.
# A zero-size inline box records the equation baseline without drawing a marker.
TEMPLATE = r"""
#import "mitex/lib.typ": mitex-convert, mitex-scope
#set page(width: auto, height: auto, margin: 1pt, fill: none)
#set par(leading: 0pt, spacing: 0pt)
#let job = json("job.json")
#set text(font: "New Computer Modern Math", size: job.size * 1pt,
          fill: rgb(..job.color.map(c => c / 255 * 100%)))
#for (index, item) in job.items.enumerate() {
  if index > 0 { pagebreak() }
  let source = if item.language == "latex" { mitex-convert(item.source) } else { item.source }
  // MiTeX maps tag to mitexlabel, whose upstream handler intentionally drops it.
  // Keep it in the equation, separated by a conventional 1.5em gap.
  let scope = (std: none)
  if item.language == "latex" {
    // These upstream handlers call eval on their dimension argument.
    let unsupported-spacing(..args) = panic("unsupported LaTeX spacing command")
    scope = mitex-scope + scope + (
      hspace: unsupported-spacing,
      vspace: unsupported-spacing,
      raisebox: unsupported-spacing,
      mitexlabel: it => box(inset: (left: 1.5em),
        if item.tag_starred { it } else { [(#it)] }),
    )
  } else {
    assert(not source.contains("#") and not source.contains("$"),
           message: "Typst math snippets cannot contain code or markup")
  }
  let body = eval(source, mode: "math", scope: scope)
  let body = if item.display { math.display(body) } else { math.inline(body) }
  let equation = box(math.equation(block: false, body))
  let marker = label("lime-baseline-" + str(index))
  box(width: 0pt, height: 0pt, baseline: 0pt)[#metadata(index)#marker]
  equation
  context {
    let size = measure(equation)
    assert(size.width < 8190pt and size.height < 8190pt, message: "equation exceeds size limit")
    assert((size.width / 1pt + 2) * (size.height / 1pt + 2) * 4 <= 8000000,
           message: "equation exceeds pixel budget")
    metadata((lime: true, index: index,
      width: size.width / 1pt + 2, height: size.height / 1pt + 2,
      baseline: query(marker).first().location().position().y / 1pt))
  }
}
"""


def typst_math_source(source: str) -> str:
    """Constrain math input and encode literal hashes and dollars before evaluation."""

    def escape_string_token(match: re.Match[str]) -> str:
        if match[1]:
            # Typst preserves the backslash in unknown string escapes like \#.
            return r"\\" + MATH_LITERAL[match[1]]
        return MATH_LITERAL.get(match[0], match[0])

    result = []
    position = 0
    while position < len(source):
        char = source[position]
        if source.startswith("//", position):
            comment = TYPST_LINE_COMMENT.match(source, position)
            result.append(comment[0].translate(MATH_LITERAL_TRANSLATION))
            position = comment.end()
        elif source.startswith("/*", position):
            depth = 1
            start = position
            position += 2
            while depth:
                marker = TYPST_COMMENT_MARKER.search(source, position)
                if marker is None:
                    raise MathError("unclosed Typst block comment")
                depth += 1 if marker[0] == "/*" else -1
                position = marker.end()
            result.append(source[start:position].translate(MATH_LITERAL_TRANSLATION))
        elif char == '"':
            literal = TYPST_STRING.match(source, position)
            if literal is None:
                raise MathError("unclosed Typst string")
            result.append(re.sub(r"\\([#$])|\\[\s\S]|[#$]", escape_string_token, literal[0]))
            position = literal.end()
        elif char == "\\" and position + 1 < len(source) and not source[position + 1].isspace():
            escaped = source[position + 1]
            result.append(MATH_LITERAL.get(escaped, char + escaped))
            position += 2
        elif char in MATH_LITERAL:
            raise MathError("Typst math snippets cannot contain code or markup")
        elif char == "s" and TYPST_STD.match(source, position):
            raise MathError("Typst math snippets cannot access std")
        else:
            result.append(char)
            position += 1
    return "".join(result)


def validate(expression: Expression) -> str:
    """Validate bounded equation input and return its typesetting source."""
    source = expression.source
    if not source.strip():
        raise MathError("empty equation")
    if len(source.encode("utf-8")) > MAX_SOURCE:
        raise MathError("equation exceeds 32 KiB")
    if expression.language == "latex":
        # MiTeX deliberately ignores some document commands. Refuse them instead
        # of presenting a successful-looking image with missing information.
        commands = {match[0][1:] for match in latex_commands(source)}
        tags = latex_tags(source)
        if tags and not expression.display:
            raise MathError("\\tag requires display math")
        if len(tags) > 1:
            raise MathError("multiple equation tags are not supported in one block")
        ignored = commands & {
            "label",
            "ref",
            "eqref",
            "cite",
            "includegraphics",
            "caption",
            "hline",
            "vline",
            "hskip",
            "mskip",
            "kern",
            "mkern",
            "mathstrut",
            "hspace",
            "vspace",
            "raisebox",
            "Huge",
            "huge",
            "Large",
            "LARGE",
            "large",
            "small",
            "tiny",
            "normalsize",
            "footnotesize",
            "scriptsize",
            "if",
            "ifdim",
            "ifnum",
            "iffalse",
            "iftrue",
            "ifx",
            "newenvironment",
            "renewenvironment",
            "documentclass",
            "RequirePackage",
        }
        if ignored:
            raise MathError("unsupported equation commands: " + ", ".join(sorted(ignored)))
        if commands & {"input", "include", "write", "openout", "read", "iftypst", "usepackage"}:
            raise MathError("only self-contained LaTeX equations are supported")
        return source
    elif expression.language == "typst":
        return typst_math_source(source)
    else:
        raise MathError(f"unknown math language: {expression.language}")


def equation_job(expression: Expression) -> dict[str, str | bool]:
    source = validate(expression)
    tags = latex_tags(source) if expression.language == "latex" else []
    starred = bool(tags and source[tags[0].end() :].startswith("*"))
    if starred:
        position = tags[0].end()
        source = source[:position] + source[position + 1 :]
    return {
        "source": source,
        "language": expression.language,
        "display": expression.display,
        "tag_starred": starred,
    }


class TypstBackend:
    """Compile a bounded batch to PNG and numeric baseline metadata.

    MiTeX is vendored and pinned, so reading math never downloads a package.
    Both subprocesses have deadlines; no source is passed through a shell.
    """

    def __init__(self, command: str | None = None) -> None:
        self.command = command or shutil.which("typst")
        self.deadline: float | None = None

    @property
    def identity(self) -> tuple:
        if not self.command:
            return (None,)
        try:
            stat = Path(self.command).stat()
            return (self.command, stat.st_mtime_ns, stat.st_size, "mitex-0.2.7", 2)
        except OSError:
            return (self.command,)

    def _temporary_parent(self) -> Path | None:
        if self.command and Path(self.command).resolve().name == "snap":
            # Strict snaps have a private /tmp. Their per-user common directory
            # is visible to both Lime and the confined Typst process.
            directory = Path.home() / "snap" / "typst" / "common"
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        return None

    def _run(self, arguments: list[str], directory: Path) -> bytes:
        if not self.command:
            raise MathError("install Typst to render math")
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("_math_worker.py")),
                        self.command,
                        *arguments,
                        "--jobs",
                        "2",
                        "--ignore-system-fonts",
                    ],
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=errors,
                    timeout=min(TIMEOUT, max(0.01, self.deadline - time.monotonic()))
                    if self.deadline
                    else TIMEOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise MathError("math renderer timed out") from None
            except OSError as error:
                raise MathError(f"cannot run Typst: {error}") from error
            errors.seek(0)
            diagnostic = clean_text(errors.read(8192).decode("utf-8", errors="replace"))
            if result.returncode:
                # Keep the actual first error, without exposing a page of temp paths.
                message = next(
                    (line for line in diagnostic.splitlines() if line.startswith("error:")),
                    "Typst could not render equation",
                )
                message = message.removeprefix("error: ")
                message = message.removeprefix("plugin errored with: error: ")
                raise MathError(message[:300])
            output.seek(0)
            data = output.read(2_000_001)
            if len(data) > 2_000_000:
                raise MathError("math metadata exceeds output budget")
            return data

    def render(self, expressions: list[Expression], size: float, color: RGB) -> list[MathImage]:
        if not expressions:
            return []
        if len(expressions) > MAX_BATCH:
            raise MathError("math batch exceeds 32 equations")
        items = [equation_job(expression) for expression in expressions]
        with tempfile.TemporaryDirectory(
            prefix="lime-math-", dir=self._temporary_parent()
        ) as temporary:
            directory = Path(temporary)
            shutil.copytree(VENDOR, directory / "mitex")
            (directory / "math.typ").write_text(TEMPLATE, encoding="utf-8")
            (directory / "job.json").write_text(
                json.dumps(
                    {
                        "size": size,
                        "color": color,
                        "items": items,
                    }
                ),
                encoding="utf-8",
            )
            # Query first: the template enforces dimensions before rasterization.
            raw = self._run(
                [
                    "eval",
                    "--root",
                    str(directory),
                    "--in",
                    "math.typ",
                    "query(metadata).map(it => it.value)",
                ],
                directory,
            )
            try:
                metadata = [
                    item
                    for item in json.loads(raw)
                    if isinstance(item, dict) and item.get("lime") is True
                ]
            except (ValueError, TypeError):
                raise MathError("invalid math renderer metadata") from None
            if len(metadata) != len(expressions):
                raise MathError("math renderer returned incomplete metrics")
            for item in metadata:
                if (
                    not all(
                        isinstance(item.get(key), (int, float)) and math.isfinite(item[key])
                        for key in ("width", "height", "baseline")
                    )
                    or not 0 < item["width"] * item["height"] * 4 <= MAX_PIXELS
                    or max(item["width"], item["height"]) * 2 > MAX_DIMENSION
                ):
                    raise MathError("equation exceeds image budget")
            self._run(
                [
                    "compile",
                    "--root",
                    str(directory),
                    "math.typ",
                    "equation-{p}.png",
                    "--ppi",
                    "144",
                ],
                directory,
            )
            images = []
            for index, item in enumerate(metadata):
                path = directory / f"equation-{index + 1}.png"
                if not path.is_file() or path.stat().st_size > 16_000_000:
                    raise MathError("math renderer returned an invalid image")
                png = path.read_bytes()
                with Image.open(io.BytesIO(png)) as image:
                    if image.width * image.height > MAX_PIXELS:
                        raise MathError("equation exceeds pixel budget")
                    image.verify()
                    images.append(MathImage(png, image.width, image.height, item["baseline"] * 2))
            return images


class MathRenderer:
    """Session cache and batch isolation; a bad equation never poisons its neighbours."""

    def __init__(
        self,
        cell_height: int,
        foreground: RGB,
        *,
        backend: TypstBackend | None = None,
        cache: ImageCache[MathImage] | None = None,
    ) -> None:
        self.backend = backend or TypstBackend()
        self.foreground = foreground
        # PNG is rendered at 2 pixels per point and displayed at 2x supersampling.
        self.size = max(6, cell_height * 0.85)
        self.cache = cache if cache is not None else ImageCache()
        self.failures: dict[Expression, str] = {}
        self._attempts = 0
        self._deadline = time.monotonic() + SESSION_SECONDS
        self._pending: dict[Expression, None] = {}

    def schedule(self, expressions: list[Expression]) -> None:
        """Prefetch in reading order without filling and immediately thrashing the cache."""
        self._pending.update(dict.fromkeys(expressions))

    def key(self, expression: Expression) -> tuple:
        return (expression, self.size, self.foreground, self.backend.identity)

    def prepare(self, expressions: list[Expression]) -> None:
        missing = list(
            dict.fromkeys(
                expression
                for expression in expressions
                if self.cache.get(self.key(expression)) is None and expression not in self.failures
            )
        )
        for start in range(0, len(missing), MAX_BATCH):
            self._batch(missing[start : start + MAX_BATCH])

    def _batch(self, expressions: list[Expression]) -> None:
        if not expressions:
            return
        # Bound repeated failures in very large or adversarial documents.
        if self._attempts >= 64 or time.monotonic() >= self._deadline:
            for expression in expressions:
                self.failures[expression] = "math render budget exhausted; showing source"
            return
        self._attempts += 1
        self.backend.deadline = self._deadline
        try:
            rendered = self.backend.render(expressions, self.size, self.foreground)
        except (OSError, ValueError) as error:
            if len(expressions) > 1 and self.backend.command:
                middle = len(expressions) // 2
                self._batch(expressions[:middle])
                self._batch(expressions[middle:])
            else:
                for expression in expressions:
                    self.failures[expression] = clean_text(str(error))
            return
        for expression, image in zip(expressions, rendered, strict=True):
            self.cache.put(self.key(expression), image, len(image.png))

    def get(self, expression: Expression) -> MathImage:
        cached = self.cache.get(self.key(expression))
        if cached is None and expression not in self.failures:
            batch = [expression]
            batch.extend(
                islice(
                    (
                        item
                        for item in self._pending
                        if item != expression and self.cache.get(self.key(item)) is None
                    ),
                    max(0, min(MAX_BATCH, self.cache.max_entries) - 1),
                )
            )
            for item in batch:
                self._pending.pop(item, None)
            self.prepare(batch)
            cached = self.cache.get(self.key(expression))
        if cached is None:
            raise MathError(self.failures.get(expression, "math image exceeds cache budget"))
        return cached
