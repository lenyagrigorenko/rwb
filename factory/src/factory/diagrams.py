"""PlantUML diagrams of the generated docs rendered to SVG.

The regulation asks for PlantUML sources (editable text); GitLab and the factory UI show them as code, so next to each
source block the pipeline puts the rendered picture: docs/diagrams/<doc>-NN.puml + .svg.
"""

from __future__ import annotations

import re
from pathlib import Path

from .builder import run

IMAGE = "plantuml/plantuml:1.2024.8"
BLOCK = re.compile(r"```plantuml\n(.*?)```\n", re.S)


async def render(docs: Path) -> int:
    """Renders PlantUML blocks of docs/*.md; returns the number of diagrams embedded (0 if the image is unavailable)."""
    code, _ = await run(["docker", "image", "inspect", IMAGE], timeout=30)
    if code != 0:
        code, _ = await run(["docker", "pull", "-q", IMAGE], timeout=300)
        if code != 0:
            return 0
    out = docs / "diagrams"
    out.mkdir(exist_ok=True)
    pending: dict[Path, list[tuple[str, str]]] = {}
    for md in sorted(docs.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        blocks = BLOCK.findall(text)
        if not blocks:
            continue
        names = []
        for i, body in enumerate(blocks, 1):
            name = f"{md.stem}-{i:02d}"
            (out / f"{name}.puml").write_text(body, encoding="utf-8")
            names.append((name, body))
        pending[md] = names
    if not pending:
        return 0
    code, log = await run(["docker", "run", "--rm", "-v", f"{out.resolve()}:/data", IMAGE, "-tsvg", "-charset", "UTF-8",
                           "/data/*.puml"], timeout=300)
    count = 0
    for md, names in pending.items():
        text = md.read_text(encoding="utf-8")
        it = iter(names)

        def embed(m: re.Match) -> str:
            nonlocal count
            name, _ = next(it)
            if not (out / f"{name}.svg").exists():
                return m.group(0)
            count += 1
            return m.group(0) + f"\n![{name}](diagrams/{name}.svg)\n"
        md.write_text(BLOCK.sub(embed, text), encoding="utf-8")
    return count
