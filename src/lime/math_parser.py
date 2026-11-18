"""Markdown math rules, preserving container indentation and escaped delimiters."""

import re

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.rules_inline import StateInline

BLOCK_BOUNDARY = re.compile(r"^(?:#{1,6}(?:\s|$)|~{3,}|`{3,}|\\\[)")


def escaped(source: str, position: int) -> bool:
    count = 0
    while position > 0 and source[position - 1] == "\\":
        count += 1
        position -= 1
    return count % 2 == 1


def closing_delimiter(source: str, delimiter: str, start: int, end: int) -> int:
    while (position := source.find(delimiter, start, end)) >= 0:
        if not escaped(source, position):
            return position
        start = position + 1
    return -1


def dollar_inline(state: StateInline, silent: bool) -> bool:
    # Markdown-it probes inline rules while finding the end of a link label.
    # Let the link see its own closing bracket before deciding where math ends.
    if silent:
        return False
    source, start, end = state.src, state.pos, state.posMax
    if source[start] != "$" or start + 1 >= end:
        return False
    previous = source[start - 1] if start else ""
    if previous == "$" or previous.isdigit() or escaped(source, start):
        return False
    if source[start + 1] == "$" or source[start + 1].isspace():
        return False
    close = closing_delimiter(source, "$", start + 1, end)
    if close <= start + 1 or source[close - 1].isspace():
        return False
    if close + 1 < end:
        following = source[close + 1]
        if following == "$" or following.isdigit():
            return False
    token = state.push("math_inline", "math", 0)
    token.content, token.markup = source[start + 1 : close], "$"
    state.pos = close + 1
    return True


def bracket_inline(state: StateInline, silent: bool) -> bool:
    if silent or not state.src.startswith("\\(", state.pos, state.posMax):
        return False
    start = state.pos + 2
    stop = closing_delimiter(state.src, "\\)", start, state.posMax)
    if stop <= start:
        return False
    token = state.push("math_inline", "math", 0)
    token.content, token.markup = state.src[start:stop], "\\("
    state.pos = stop + 2
    return True


def math_block(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
    if state.is_code_block(start_line):
        return False
    start = state.bMarks[start_line] + state.tShift[start_line]
    opening = state.src[start : start + 2]
    if opening not in ("$$", "\\["):
        return False
    closing = "$$" if opening == "$$" else "\\]"
    for stop in range(start_line, end_line):
        begin = state.bMarks[stop] + state.tShift[stop]
        line = state.src[begin : state.eMarks[stop]].rstrip()
        if stop > start_line and (
            not line or state.sCount[stop] < state.blkIndent or BLOCK_BOUNDARY.match(line)
        ):
            return False
        close = closing_delimiter(line, closing, 2 if stop == start_line else 0, len(line))
        if close >= 0:
            if close + len(closing) != len(line):
                return False
            if silent:
                return True
            # getLines uses the parser's adjusted offsets for list/quote prefixes.
            # A raw source slice would accidentally include > from later lines.
            body = state.getLines(start_line, stop + 1, state.blkIndent, False).strip()
            token = state.push("math_block", "math", 0)
            token.content, token.markup = body[2:-2], opening
            token.block, token.map = True, [start_line, stop + 1]
            state.line = stop + 1
            return True
    return False


def math_plugin(markdown: MarkdownIt) -> None:
    markdown.block.ruler.before(
        "fence",
        "math_block",
        math_block,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    markdown.inline.ruler.before("escape", "math_inline", dollar_inline)
    markdown.inline.ruler.before("escape", "math_bracket_inline", bracket_inline)
