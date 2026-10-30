"""docx·hwpx 회의록 렌더: 합성 회의록 JSON → 양식별 docx·hwpx → 글자 추출로 필드 확인.

hwpx는 표준 라이브러리만 쓰므로 늘 돈다. docx는 extra `office`(python-docx, docxtpl)가 있을 때만.
pdf·png 변환은 외부 도구(rhwp, rsvg-convert, LibreOffice)가 있을 때만 확인한다.
"""

from __future__ import annotations

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
        assert a.read("Contents/header.xml") == b.read("Contents/header.xml")
    head = src[: src.index("<hp:tbl ")]
    assert dst.startswith(head.replace("{{doc_title}}", t.doc_title(minutes)))
    assert " />" not in dst  # ElementTree식 빈 태그가 끼어들지 않음


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


def test_rhwp_workaround_only_multi_paragraph_top_cells():
    xml = ('<hp:subList vertAlign="TOP"><hp:p a="1"></hp:p><hp:p a="2"></hp:p></hp:subList>'
           '<hp:subList vertAlign="TOP"><hp:p a="1"></hp:p></hp:subList>')
    out = hwpx._rhwp_workaround(xml)
    assert out.count('vertAlign="CENTER"') == 1 and out.count('vertAlign="TOP"') == 1


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

    t = load_template("kr-standard")
    text = docx_text(render_docx(minutes, t, tmp_path / "out.docx"))
    assert f"{t.doc_title(minutes)} / {minutes.place}" in text
    assert minutes.attendees[0].name in text
    first = t.content_lines(minutes)[0]
    assert f"[0]{first.prefix}{first.text}" in text
    assert "<태그>, & 기호" in text
    assert not re.search(r"\{\{|\{%", text)


@pytest.mark.skipif(not shutil.which("soffice") and not Path("/opt/homebrew/bin/soffice").exists(),
                    reason="LibreOffice 없음")
def test_docx_export(minutes, tmp_path):
    pytest.importorskip("docx")
    from rec2md.render.docx import render_docx, to_pdf

    out = render_docx(minutes, load_template("ji"), tmp_path / "ji.docx")
    assert to_pdf(out, tmp_path / "ji.pdf").read_bytes().startswith(b"%PDF")
