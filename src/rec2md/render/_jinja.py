"""md·html 렌더러 공용: 양식 폴더를 검색 경로로 쓰는 Jinja2 환경과 템플릿 문맥."""

from __future__ import annotations

from ..minutes import Minutes
from ..templates import Field, Template


def environment(t: Template, autoescape: bool):
    try:
        import jinja2
    except ModuleNotFoundError as e:
        raise SystemExit("md·html 렌더에는 Jinja2가 필요함: uv sync --extra render "
                         "(또는 pip install 'rec2md[render]')") from e
    return jinja2.Environment(loader=jinja2.FileSystemLoader([str(d) for d in t.dirs]),
                              autoescape=autoescape, trim_blocks=True, lstrip_blocks=True,
                              keep_trailing_newline=True, undefined=jinja2.StrictUndefined)


def context(m: Minutes, t: Template) -> dict:
    """템플릿에 넘기는 값. 사용자 page.*.j2도 이 이름들을 쓴다 (docs/templates.md)."""
    values = t.values(m)
    rows = [[(f, values[f.value]) for f in row] for row in t.rows()]
    return {
        "m": m, "t": t, "title": t.doc_title(m), "values": values,
        "rows": rows,                                   # [[(Field, [줄])]]
        "fields": [cell for row in rows for cell in row],
        "content": t.content_lines(m),                  # [Line]
    }


def colspans(row: list[tuple[Field, list[str]]], width: int) -> list[int]:
    """row 한 줄의 값 칸 colspan. 표 전체 폭 = width쌍(라벨+값), 남는 칸은 마지막 값 칸이 차지."""
    k = len(row)
    free = 2 * width - k                # 라벨 칸 k개를 뺀 나머지
    base = free // k
    return [base] * (k - 1) + [free - base * (k - 1)]
