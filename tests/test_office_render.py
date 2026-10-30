"""docx·hwpx 회의록 렌더: 합성 회의록 JSON → 양식별 docx·hwpx → 글자 추출로 필드 확인.

hwpx는 표준 라이브러리만 쓰므로 늘 돈다. docx는 extra `office`(python-docx, docxtpl)가 있을 때만.
pdf·png 변환은 외부 도구(rhwp, rsvg-convert, LibreOffice)가 있을 때만 확인한다.
"""

from __future__ import annotations

import html
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from rec2md.minutes import Minutes
from rec2md.render import hwpx
from rec2md.render.hwpx import blank_form, build_form, fill_form, render_hwpx
from rec2md.templates import load_template

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "minutes_render.json"
TEMPLATES = ["ji", "kr-standard"]
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"


@pytest.fixture(autouse=True)
def no_user_templates(tmp_path, monkeypatch):
    """~/.config/rec2md/templates의 개인 양식(회사 원본 hwpx 등)이 결과를 바꾸지 않게."""
    monkeypatch.setenv("REC2MD_TEMPLATES", str(tmp_path / "user-templates"))


@pytest.fixture
def minutes() -> Minutes:
    return Minutes.from_json(FIXTURE)


def hwpx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("Contents/section0.xml").decode("utf-8")
    root = ET.fromstring(xml)  # 잘 짜인 XML인지도 같이 본다
    return "\n".join("".join(t.itertext()) for t in root.iter(f"{HP}t"))


def expected_lines(m: Minutes, t) -> list[str]:
    vals = t.values(m)
    return [s for f in t.fields for s in (vals[f.value] if f.value != "content" else
                                          [str(x) for x in t.content_lines(m)])]


# ── hwpx ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", TEMPLATES)
def test_hwpx_builtin_form(minutes, tmp_path, name):
    """원본 hwpx가 없으면 [[fields]] 배치로 표 양식을 만들어 채운다."""
    t = load_template(name)
    out = render_hwpx(minutes, t, tmp_path / f"{name}.hwpx")
    text = hwpx_text(out)
    assert t.doc_title(minutes) in text
    for f in t.fields:
        assert f.label in text
    for line in expected_lines(minutes, t):
        assert line in text, line
    assert "<태그>, & 기호" in text  # 이스케이프가 왕복한다
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        assert (first.filename, first.compress_type) == ("mimetype", zipfile.ZIP_STORED)
        assert z.read("mimetype") == b"application/hwp+zip"
        assert z.testzip() is None


def test_hwpx_fill_user_form(minutes, tmp_path, monkeypatch):
    """사용자 폴더의 form.hwpx를 라벨로 찾아 채운다. 채운 결과에서 값을 비우면 다시 양식으로 쓸 수 있다."""
    t = load_template("kr-standard")
    filled = render_hwpx(minutes, t, tmp_path / "filled.hwpx")
    labels = {f.label: "right" for f in t.fields}
    form = blank_form(filled, labels, tmp_path / "form.hwpx", markers={"회의내용": ["1.", "-", "·"]})
    blank = hwpx_text(form)
    assert minutes.place not in blank and minutes.attendees[0].name not in blank
    for f in t.fields:
        assert f.label in blank

    user = tmp_path / "user-templates" / "kr-standard"
    user.mkdir(parents=True)
    shutil.copy(form, user / "form.hwpx")
    t2 = load_template("kr-standard")
    assert t2.file("hwpx") == user / "form.hwpx"
    text = hwpx_text(render_hwpx(minutes, t2, tmp_path / "again.hwpx"))
    for line in expected_lines(minutes, t2):
        assert line in text, line


def test_hwpx_values_with_placeholder_text(minutes, tmp_path):
    """회의 값에 든 `{{title}}` 같은 글자는 자리표시자로 치지 않는다 (자리표시자는 양식에서만 바꾼다)."""
    minutes.place = "Discuss {{title}} and {{place}} placeholders"
    t = load_template("kr-standard")
    text = hwpx_text(render_hwpx(minutes, t, tmp_path / "out.hwpx"))
    assert minutes.place in text
    assert t.doc_title(minutes) in text  # 양식의 {{doc_title}}은 바뀐다


def test_hwpx_missing_label(minutes, tmp_path):
    t = load_template("kr-standard")
    form = build_form(t, tmp_path / "form.hwpx")
    with pytest.raises(KeyError, match="없는칸"):
        fill_form(form, {"없는칸": ("right", [(0, "x")])}, tmp_path / "out.hwpx")
    fill_form(form, {"없는칸": ("right", [(0, "x")])}, tmp_path / "out.hwpx", required=False)


def test_hwpx_section_is_spliced_not_reserialized(minutes, tmp_path):
    """채운 칸 밖의 XML은 원문 그대로다 (한글은 다시 직렬화한 section을 변조로 의심한다)."""
    t = load_template("kr-standard")
    form = build_form(t, tmp_path / "form.hwpx")
    out = render_hwpx(minutes, t, tmp_path / "out.hwpx")
    with zipfile.ZipFile(form) as a, zipfile.ZipFile(out) as b:
        src, dst = a.read("Contents/section0.xml").decode(), b.read("Contents/section0.xml").decode()
        h_src, h_dst = a.read("Contents/header.xml").decode(), b.read("Contents/header.xml").decode()
    # header는 내어쓰기 문단 모양을 덧붙인 것(과 그 개수) 말고는 원문 그대로다
    last = max(int(i) for i in re.findall(r'<hh:paraPr id="(\d+)"', h_src))
    added = "".join(m.group(0) for m in re.finditer(r'<hh:paraPr id="(\d+)".*?</hh:paraPr>', h_dst, re.S)
                    if int(m.group(1)) > last)
    assert added
    strip = lambda h: re.sub(r'(<hh:paraProperties itemCnt=")\d+', r"\1", h)  # noqa: E731
    assert strip(h_dst.replace(added, "")) == strip(h_src)
    head = src[: src.index("<hp:tbl ")]
    assert dst.startswith(head.replace("{{doc_title}}", t.doc_title(minutes)))
    assert " />" not in dst  # ElementTree식 빈 태그가 끼어들지 않음


def test_hwpx_cell_height_matches_preview_wrap(minutes, tmp_path):
    """칸 높이 어림과 미리보기 줄바꿈이 같은 계산이라, 공백에서 끊겨 늘어난 줄도 칸 안에 든다 (Codex 리뷰 #18)."""
    from rec2md.minutes import Item, Section

    word = "가" * 23
    minutes.summary, minutes.decisions, minutes.actions, minutes.next_meeting = [], [], [], ""
    minutes.sections = [Section("긴 단어", [Item(" ".join([word] * 8)), Item("짧은 줄")])]
    t = load_template("kr-standard")
    entries = hwpx._read_zip(render_hwpx(minutes, t, tmp_path / "out.hwpx"))
    xml = entries["Contents/section0.xml"].decode("utf-8")
    header = entries["Contents/header.xml"]
    cached = hwpx._layout_cache(xml, hwpx._metrics(header))
    root = hwpx._tree(cached)
    cell = next(tc for tc in hwpx._walk(root, "tc") if word in hwpx._cell_text(cached, tc))
    height = int(hwpx._attrs(hwpx._own(cached, cell, "cellSz").group(0))["height"])
    last = re.findall(r'vertpos="(\d+)" vertsize="(\d+)"', cached[cell.start:cell.end])[-1]
    assert int(last[0]) + int(last[1]) <= height


def test_hwpx_hanging_indent(minutes, tmp_path):
    """머리기호가 있는 줄은 기호 폭만큼 내어쓰는 문단 모양(intent 음수)을 쓴다. 같은 모양은 한 번만 만든다."""
    t = load_template("ji")
    out = render_hwpx(minutes, t, tmp_path / "out.hwpx")
    with zipfile.ZipFile(out) as z:
        header, sec = z.read("Contents/header.xml").decode(), z.read("Contents/section0.xml").decode()
    intents = {i: int(v) for i, v in re.findall(
        r'<hh:paraPr id="(\d+)".*?<hc:intent value="(-?\d+)"', header, re.S)}
    lines = {str(x): x for x in t.content_lines(minutes)}
    seen = 0
    for ppr, text in re.findall(r'<hp:p [^>]*paraPrIDRef="(\d+)"[^>]*><hp:run[^>]*><hp:t>([^<]*)</hp:t>', sec):
        x = lines.get(html.unescape(text))
        if x is not None and x.prefix.strip():
            assert intents[ppr] < 0, text
            seen += 1
    assert seen >= len([x for x in lines.values() if x.prefix.strip()])
    hanging = [i for i, v in intents.items() if v < 0]
    assert len(hanging) == len(set(hanging))


def test_docx_hanging_indent(minutes, tmp_path):
    docx = pytest.importorskip("docx")
    from rec2md.render.docx import render_docx

    out = render_docx(minutes, load_template("ji"), tmp_path / "ji.docx")
    paras = [p for tbl in docx.Document(str(out)).tables for row in tbl.rows for c in row.cells for p in c.paragraphs]
    marked = [p for p in paras if p.text.startswith(("▶ ", "- "))]
    assert marked and all(p.paragraph_format.first_line_indent < 0 for p in marked)
    assert all(p.paragraph_format.left_indent >= -p.paragraph_format.first_line_indent for p in marked)


def test_hwpx_below_and_nested_tables():
    """라벨 아래 칸(where=below) 찾기, 안쪽 표의 칸은 바깥 표의 이웃으로 치지 않기."""
    def tc(r, c, text, inner="", rs=1, cs=1):
        return (f'<hp:tc><hp:subList><hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:t>{text}</hp:t>{inner}'
                f'</hp:run></hp:p></hp:subList><hp:cellAddr colAddr="{c}" rowAddr="{r}"/>'
                f'<hp:cellSpan colSpan="{cs}" rowSpan="{rs}"/><hp:cellSz width="1000" height="1000"/></hp:tc>')

    inner = '<hp:tbl>' + '<hp:tr>' + tc(0, 0, "내용") + tc(0, 1, "안쪽") + '</hp:tr></hp:tbl>'
    xml = ('<hs:sec><hp:p><hp:run><hp:tbl><hp:tr>' + tc(0, 0, "회의내용", rs=2) + tc(0, 1, "내 용") + '</hp:tr>'
           '<hp:tr>' + tc(1, 1, "", inner=inner) + '</hp:tr></hp:tbl></hp:run></hp:p></hs:sec>')
    root = hwpx._tree(xml)
    got = hwpx._targets(xml, root, {"내용": "below", "회의내용": "right"})
    assert hwpx._addr(xml, got["내용"])[:2] == (1, 1)  # 바깥 표의 "내 용" 아래 칸
    assert hwpx._addr(xml, got["회의내용"])[:2] == (0, 1)


def test_hwpx_preview_is_scrubbed(minutes, tmp_path):
    """양식 원본의 미리보기(채워진 회의록일 수 있음)가 결과에 남지 않는다."""
    t = load_template("kr-standard")
    out = render_hwpx(minutes, t, tmp_path / "out.hwpx")
    with zipfile.ZipFile(out) as z:
        assert z.read("Preview/PrvImage.png").startswith(b"\x89PNG")
        assert minutes.place in z.read("Preview/PrvText.txt").decode("utf-8")


def test_wrap_breaks_at_spaces():
    # 글자 크기 1000, 폭 4000: 한글 4자가 한 줄
    assert hwpx._wrap("가나 다라마바", 1000, 4000) == [0, 3]
    assert hwpx._wrap("가나다라마바사", 1000, 4000) == [0, 4]  # 공백이 없으면 글자에서
    assert hwpx._wrap("", 1000, 4000) == [0]


def test_layout_cache_only_in_preview_and_multi_paragraph_cells(minutes, tmp_path):
    """미리보기 사본에서만, 문단이 여럿인 칸의 캐시 없는 문단에 어림한 linesegarray를 넣는다."""
    t = load_template("kr-standard")
    out = render_hwpx(minutes, t, tmp_path / "out.hwpx")
    entries = hwpx._read_zip(out)
    xml = entries["Contents/section0.xml"].decode("utf-8")
    assert "<hp:linesegarray>" not in xml  # 결과 hwpx에는 넣지 않는다
    metrics = hwpx._metrics(entries["Contents/header.xml"])
    cached = hwpx._layout_cache(xml, metrics)
    root = hwpx._tree(cached)
    for tc in hwpx._walk(root, "tc"):
        ps = hwpx._paras(tc)
        assert all((hwpx._own_lineseg(cached, p) is not None) == (len(ps) > 1) for p in ps)
    assert hwpx._layout_cache(cached, metrics) == cached  # 두 번 넣지 않음
    ET.fromstring(cached)


@pytest.mark.skipif(not (hwpx.find_rhwp() and shutil.which("rsvg-convert")), reason="rhwp, rsvg-convert 없음")
def test_hwpx_export(minutes, tmp_path):
    out = render_hwpx(minutes, load_template("ji"), tmp_path / "ji.hwpx")
    pngs = hwpx.to_png(out, tmp_path)
    assert pngs and all(p.read_bytes().startswith(b"\x89PNG") for p in pngs)
    assert hwpx.to_pdf(out, tmp_path / "ji.pdf").read_bytes().startswith(b"%PDF")


# ── docx ───────────────────────────────────────────────────────────────

def docx_text(path: Path) -> str:
    docx = pytest.importorskip("docx")
    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts += [p.text for p in cell.paragraphs]
    return "\n".join(parts)


@pytest.mark.parametrize("name", TEMPLATES)
def test_docx_built(minutes, tmp_path, name):
    pytest.importorskip("docx")
    from rec2md.render.docx import render_docx

    t = load_template(name)
    text = docx_text(render_docx(minutes, t, tmp_path / f"{name}.docx"))
    assert t.doc_title(minutes) in text
    for f in t.fields:
        assert f.label in text
    for line in expected_lines(minutes, t):
        assert line in text, line


def test_docx_user_form(minutes, tmp_path):
    """사용자 양식 form.docx가 있으면 docxtpl로 채운다."""
    docx = pytest.importorskip("docx")
    pytest.importorskip("docxtpl")
    from rec2md.render.docx import render_docx

    user = tmp_path / "user-templates" / "kr-standard"
    user.mkdir(parents=True)
    form = docx.Document()
    form.add_paragraph("{{ doc_title }} / {{ place }}")
    form.add_paragraph("참석: {{ attendees }}")
    form.add_paragraph("{% for x in content %}[{{ x.level }}]{{ x.prefix }}{{ x.text }} {% endfor %}")
    form.save(str(user / "form.docx"))

    minutes.place = "Discuss {{title}} and {% if x %} placeholders"  # 값 안의 템플릿 문법은 그대로 남아야 한다
    t = load_template("kr-standard")
    text = docx_text(render_docx(minutes, t, tmp_path / "out.docx"))
    assert f"{t.doc_title(minutes)} / {minutes.place}" in text
    assert minutes.attendees[0].name in text
    first = t.content_lines(minutes)[0]
    assert f"[0]{first.prefix}{first.text}" in text
    assert "<태그>, & 기호" in text
    assert not re.search(r"\{\{|\{%", text.replace(minutes.place, ""))


@pytest.mark.skipif(not shutil.which("soffice") and not Path("/opt/homebrew/bin/soffice").exists(),
                    reason="LibreOffice 없음")
def test_docx_export(minutes, tmp_path):
    pytest.importorskip("docx")
    from rec2md.render.docx import render_docx, to_pdf

    out = render_docx(minutes, load_template("ji"), tmp_path / "ji.docx")
    assert to_pdf(out, tmp_path / "ji.pdf").read_bytes().startswith(b"%PDF")
