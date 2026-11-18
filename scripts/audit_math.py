"""Render the scientific corpus for manual typography review.

Run: uv run python scripts/audit_math.py --latex-reference
Outputs are ignored build artifacts. The optional reference uses an installed
TeX distribution and dvipng; neither is needed by Lime's runtime renderer.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import time
from pathlib import Path

from lime.math_render import Expression, TypstBackend


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".build/math-audit"))
    parser.add_argument("--latex-reference", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    corpus = json.loads((Path(__file__).parents[1] / "tests/fixtures/math-corpus.json").read_text())
    backend = TypstBackend()
    expressions = [
        Expression(item[key], language, True)
        for item in corpus
        for key, language in (("latex", "latex"), ("typst", "typst"))
    ]
    started = time.perf_counter()
    images = backend.render(expressions, 13, (220, 220, 220))
    elapsed = time.perf_counter() - started
    reference = args.latex_reference
    if reference:
        if not shutil.which("latex") or not shutil.which("dvipng"):
            parser.error("--latex-reference needs latex and dvipng")
        pages = ["{\\[" + item["latex"] + "\\]}\\newpage" for item in corpus]
        tex = (
            r"\documentclass{article}\usepackage{amsmath,amssymb}\pagestyle{empty}"
            r"\begin{document}" + "\n".join(pages) + r"\end{document}"
        )
        (output / "reference.tex").write_text(tex)
        for command in (
            [
                "latex",
                "-no-shell-escape",
                "-halt-on-error",
                "-interaction=batchmode",
                "reference.tex",
            ],
            [
                "dvipng",
                "-T",
                "tight",
                "-bg",
                "Transparent",
                "-fg",
                "rgb 0.86 0.86 0.86",
                "-D",
                "144",
                "-o",
                "reference-%d.png",
                "reference.dvi",
            ],
        ):
            result = subprocess.run(
                command, cwd=output, capture_output=True, timeout=30, check=False
            )
            if result.returncode:
                raise SystemExit(result.stdout.decode(errors="replace")[-2000:])
    rows, report = [], []
    for index, item in enumerate(corpus):
        pair = images[index * 2 : index * 2 + 2]
        cells = [f"<th>{html.escape(item['name'])}</th>"]
        for language, image in zip(("latex", "typst"), pair, strict=True):
            name = f"{item['name']}-{language}.png"
            (output / name).write_bytes(image.png)
            cells.append(f'<td><img src="{name}" alt="{language} equation"></td>')
        if reference:
            cells.append(f'<td><img src="reference-{index + 1}.png" alt="TeX reference"></td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
        report.append(
            {
                "name": item["name"],
                "pixel_identical": pair[0] == pair[1],
                "metrics": [
                    {"width": im.width, "height": im.height, "baseline": im.baseline} for im in pair
                ],
            }
        )
    document = (
        "<!doctype html><meta charset=utf-8><title>Lime math typography audit</title>"
        "<style>body{background:#181818;color:#ddd;font:16px sans-serif}"
        "td,th{padding:16px;text-align:left;border-bottom:1px solid #555}"
        "img{max-width:100%}table{border-collapse:collapse}</style>"
        "<h1>Lime math typography audit</h1>"
        "<p>Visual comparison; a compilation success is not a semantic equivalence proof.</p>"
        "<table><tr><th>Case</th><th>MiTeX → Typst</th><th>Native Typst</th>"
        + ("<th>LaTeX reference</th>" if reference else "")
        + "</tr>"
        + "".join(rows)
        + "</table>"
    )
    (output / "index.html").write_text(document)
    (output / "results.json").write_text(
        json.dumps(
            {"batch_seconds": elapsed, "latex_reference": reference, "cases": report}, indent=2
        )
    )
    print(f"Rendered {len(corpus)} pairs in {elapsed:.3f}s: {output / 'index.html'}")


if __name__ == "__main__":
    main()
