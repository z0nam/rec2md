"""Minutes → Markdown. 기본 양식은 templates/_base/page.md.j2."""

from __future__ import annotations

from pathlib import Path

from ..minutes import Minutes
from ..templates import Line, Template
from ._jinja import context, environment

BULLETS = {"-", "*", "·", "•", "+"}


def cell(lines: list[str]) -> str:
    """표 칸: 줄은 <br>, 파이프는 이스케이프."""
    return "<br>".join(x.replace("|", "\\|") for x in lines)


def content_md(lines: list[Line]) -> str:
    """회의내용을 Markdown으로. 소제목은 ###, 소항목·내용은 들여쓴 목록.
    양식 접두가 목록 기호("- ")면 Markdown 목록과 겹치므로 떼고, 아니면("▶ ") 그대로 둔다."""
    out: list[str] = []
    for ln in lines:
        if ln.level == 0:
            out += ["", f"### {ln.prefix}{ln.text}".rstrip(), ""]
            continue
        prefix = "" if ln.prefix.strip() in BULLETS else ln.prefix
        out.append("  " * (ln.level - 1) + f"- {prefix}{ln.text}")
    return "\n".join(out).strip("\n")


def render_md(m: Minutes, template: Template, out: Path) -> Path:
    src = template.file("md")
    if src is None:
        raise FileNotFoundError(f"양식 '{template.name}'에 md 원본({template.files.get('md')})이 없음")
    env = environment(template, autoescape=False)
    env.filters["cell"] = cell
    ctx = context(m, template) | {"content_md": content_md(template.content_lines(m))}
    text = env.get_template(src.name).render(**ctx)
    out.write_text(text.rstrip() + "\n", encoding="utf-8")
    return out
