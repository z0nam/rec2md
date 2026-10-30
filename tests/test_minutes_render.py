"""회의록 렌더 골든 테스트: 합성 회의록 JSON → 양식별 md·html.

골든 갱신: REC2MD_UPDATE_GOLDEN=1 uv run pytest tests/test_minutes_render.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rec2md import cmd_render, export
from rec2md.minutes import Minutes
from rec2md.templates import load_template

pytest.importorskip("jinja2")  # extra "render"

from rec2md.render.html import render_html  # noqa: E402
from rec2md.render.md import render_md  # noqa: E402

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "minutes_render.json"
GOLDEN = HERE / "golden"
TEMPLATES = ["ji", "kr-standard"]


@pytest.fixture(autouse=True)
def no_user_templates(tmp_path, monkeypatch):
    """~/.config/rec2md/templates의 개인 양식이 결과를 바꾸지 않게."""
    monkeypatch.setenv("REC2MD_TEMPLATES", str(tmp_path / "user-templates"))


@pytest.fixture
def minutes() -> Minutes:
    return Minutes.from_json(FIXTURE)


def check_golden(out: Path, name: str) -> None:
    golden = GOLDEN / name
    if os.environ.get("REC2MD_UPDATE_GOLDEN"):
        golden.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    assert out.read_text(encoding="utf-8") == golden.read_text(encoding="utf-8"), f"골든과 다름: {name}"


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("fmt", ["md", "html"])
def test_golden(minutes, tmp_path, name, fmt):
    fn = {"md": render_md, "html": render_html}[fmt]
    out = fn(minutes, load_template(name), tmp_path / f"m.{fmt}")
    check_golden(out, f"{name}.{fmt}")


def test_ji_content_levels(minutes):
    lines = [str(x) for x in load_template("ji").content_lines(minutes)]
    assert lines[:3] == ["1. 회의 개요", "- 과업 범위를 3개 분야로 확정함", "- 자료 수집은 2월 말까지 마침"]
    assert "▶ 분야 구분" in lines and "- 교통·환경·관광 3개 분야로 함" in lines
    # 회의내용 뒤에 결정사항·할 일·다음 회의가 번호를 이어 붙는다
    assert [x for x in lines if x[0].isdigit()][-3:] == ["4. 결정사항", "5. 향후 할 일", "6. 다음 회의"]
    assert "▶ 분야별 자료 수집 (담당: 김철수, 기한: 2026-02-28)" in lines


def test_ji_values(minutes):
    v = load_template("ji").values(minutes)
    assert v["datetime"] == ["2026년 1월 15일 14:00 – 15:30"]
    assert v["attendees"] == ["가나연구원", "- 홍길동, 김철수", "다라시 교통과", "- 이영희", "박민수"]


def test_html_escapes(minutes, tmp_path):
    html = render_html(minutes, load_template("ji"), tmp_path / "m.html").read_text(encoding="utf-8")
    assert "&lt;태그&gt;" in html and "<태그>" not in html


def test_user_template_dir_overrides(minutes, tmp_path, monkeypatch):
    """사용자 폴더에 원본만 두면 내장 매핑 + 사용자 원본, template.toml까지 두면 사용자 것."""
    user = tmp_path / "user"
    monkeypatch.setenv("REC2MD_TEMPLATES", str(user))
    (user / "ji").mkdir(parents=True)
    (user / "ji" / "page.md.j2").write_text("# 사용자 {{ title }}\n", encoding="utf-8")
    t = load_template("ji")
    assert t.dir.name == "ji" and t.dir != user / "ji"
    assert render_md(minutes, t, tmp_path / "a.md").read_text(encoding="utf-8") == "# 사용자 회의록\n"

    (user / "ji" / "template.toml").write_text('title = "{title}"\n', encoding="utf-8")
    t = load_template("ji")
    assert t.dir == user / "ji" and t.fields == []
    assert render_md(minutes, t, tmp_path / "b.md").read_text(encoding="utf-8") == "# 사용자 가상 도시 교통 연구 착수 회의\n"


def test_template_rejects_unknown_keys(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "template.toml").write_text('titel = "오타"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="titel"):
        load_template(d)
    (d / "template.toml").write_text('[[fields]]\nlabel = "x"\nvalue = "nope"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="nope"):
        load_template(d)


def test_template_passes_format_sections(tmp_path):
    d = tmp_path / "fmt"
    d.mkdir()
    (d / "template.toml").write_text('[hwpx]\nfont = "바탕"\n', encoding="utf-8")
    assert load_template(d).data["hwpx"] == {"font": "바탕"}


def test_cli_render(tmp_path):
    src = tmp_path / "meeting.v1.minutes.json"
    src.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    cmd_render.main([str(src), "--template", "ji", "--format", "md,html"])
    assert (tmp_path / "meeting.v1.md").is_file() and (tmp_path / "meeting.v1.html").is_file()


def test_cli_missing_renderer_writes_nothing(tmp_path, monkeypatch):
    """docx·hwpx 렌더러는 다른 PR이 붙인다. 없는 렌더러가 하나라도 있으면 아무것도 쓰지 않는다."""
    monkeypatch.setattr(cmd_render, "MINUTES_FORMATS", (*cmd_render.MINUTES_FORMATS, "nosuchfmt"))
    src = tmp_path / "x.minutes.json"
    src.write_text(json.dumps({"title": "t"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="렌더러가 아직 없음"):
        cmd_render.main([str(src), "--format", "md,nosuchfmt"])
    with pytest.raises(SystemExit, match="회의록 형식이 아님"):
        cmd_render.main([str(src), "--format", "md,pdf"])
    assert not (tmp_path / "x.md").exists()


def test_detect_kind():
    assert cmd_render.detect_kind({"segments": []}) == "transcript"
    assert cmd_render.detect_kind({"title": "t"}) == "minutes"


@pytest.mark.skipif(export.find_chrome() is None, reason="Chrome 없음")
def test_export_pdf_png(minutes, tmp_path):
    pytest.importorskip("pypdfium2")
    html = render_html(minutes, load_template("ji"), tmp_path / "m.html")
    outs = export.export(html, ["pdf", "png"], tmp_path)
    assert [o.name for o in outs] == ["m.pdf", "m.p1.png"]
    assert outs[0].read_bytes()[:5] == b"%PDF-" and outs[1].stat().st_size > 10_000


SEGMENTS = {"source": "synthetic.wav", "duration": 65.0, "engine": "local:test", "language": "ko",
            "segments": [{"start": 0.0, "end": 3.0, "text": "안녕하세요.", "speaker": "화자1"},
                         {"start": 3.5, "end": 6.0, "text": "반갑습니다.", "speaker": "화자2"}],
            "meta": {}}


def test_cli_transcript_calls_export_transcript(tmp_path, monkeypatch):
    """--kind transcript는 transcript_export.export_transcript로 넘긴다 (함수는 다른 PR)."""
    import sys
    import types

    calls = []

    def fake(t, fmt, out, names=None, title=None, recorded_at=None):
        calls.append((len(t.segments), fmt, out.name, names, title, recorded_at))
        out.write_text(fmt, encoding="utf-8")
        return out

    monkeypatch.setitem(sys.modules, "rec2md.transcript_export", types.SimpleNamespace(export_transcript=fake))
    src = tmp_path / "call.segments.json"
    src.write_text(json.dumps(SEGMENTS, ensure_ascii=False), encoding="utf-8")
    cmd_render.main([str(src), "--format", "txt,html", "--title", "합성", "--names", "화자1=홍길동"])
    assert calls == [(2, "txt", "call.txt", {"화자1": "홍길동"}, "합성", None),
                     (2, "html", "call.html", {"화자1": "홍길동"}, "합성", None)]
    with pytest.raises(SystemExit, match="녹취록 형식이 아님"):
        cmd_render.main([str(src), "--format", "hwpx"])
