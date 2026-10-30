"""Minutes → HTML (A4 인쇄 CSS 포함, pdf·png 내보내기의 원천). 기본 양식은 templates/_base/page.html.j2."""

from __future__ import annotations

from pathlib import Path

from ..minutes import Minutes
from ..templates import Template
from ._jinja import colspans, context, environment, load


def render_html(m: Minutes, template: Template, out: Path) -> Path:
    env = environment(template, autoescape=True)
    ctx = context(m, template)
    width = max((len(r) for r in ctx["rows"]), default=1)
    ctx |= {"width": width, "spans": [colspans(r, width) for r in ctx["rows"]]}
    out.write_text(load(env, template, "html").render(**ctx), encoding="utf-8")
    return out


def to_pdf(src: Path, out: Path) -> Path:
    from ..export import html_to_pdf
    return html_to_pdf(src, out)
