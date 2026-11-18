# Testing math rendering

Use Typst 0.15 or newer. The test suite also runs without Typst and skips the
tests that invoke the compiler.

```sh
uv run pytest
uv run ruff check src tests scripts
uv run python scripts/audit_math.py
```

Open `.build/math-audit/index.html` to compare MiTeX and native Typst output for
the same equations. Add `--latex-reference` to include a TeX reference if
`latex`, `amsmath`, `amssymb`, and `dvipng` are installed.

In Ghostty, run `uv run lime examples/math.md` and check inline baselines,
display equations, and source fallback. Repeat at a narrow width, after zooming,
and in a light theme. Try the same document over direct SSH to exercise graphics
and cell size discovery. To check stdin and scrollback, run:

```sh
printf '%s\n' '\frac{1}{1+x^2}' | uv run lime --format latex-math -
```

Typst reports each equation's baseline. Lime estimates the terminal text
baseline from the cell height and reserves enough rows for tall equations.
Inline text stays selectable. Use `--show-math-source` to copy an equation.

Lime passes native Typst snippets as data, rejects code and markup transitions,
and encodes literal hashes and dollars before evaluation. MiTeX's spacing
commands that evaluate their dimension argument fall back to source. Each
compiler job uses a temporary root and has time and size limits.

MiTeX 0.2.7 is bundled for offline use. Its bundled [license](../src/lime/_vendor/mitex/LICENSE)
and [provenance](../src/lime/_vendor/mitex/PROVENANCE.md) accompany the runtime files.
