"""회의 메모 ↔ 녹취 정렬 (rec2md notes). 합성 메모·합성 segments만 쓴다."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from rec2md import cmd_notes, notes
from rec2md.segments import Transcript

FIX = Path(__file__).parent / "fixtures"
MEMO, SEGS = FIX / "notes_memo.md", FIX / "notes_segments.json"


@pytest.fixture
def aligned():
    t = Transcript.from_json(SEGS)
    paras, unc = notes.align(notes.memo_lines(MEMO), t)
    return {p.text: p for p in paras}, unc


def test_memo_lines_strip_markers():
    lines = notes.memo_lines(MEMO)
    assert lines[0] == ("가상 도시 교통 연구 2차 회의 메모", "heading")
    assert ("총 사업비는 3억 원으로 확정", "text") in lines
    assert ("설문조사는 4월 15일 시작", "text") in lines   # "1. " 번호 제거
    assert ("교통량 자료 김철수", "text") in lines          # 표 행은 칸을 공백으로
    assert ("항목 담당", "text") in lines and not any(set(t) <= set("|-: ") for t, _ in lines)  # 표 구분선 제외


def test_tokens_keep_number_units():
    nums, names, words = notes.tokens("총 사업비는 3억 원으로, 2차 회의 4월 15일 30% 홍길동", {"홍길동"})
    assert nums == ["3억", "2차", "4월", "15일", "30%"]
    assert names == ["홍길동"]
    assert "사업비" in words and "원으" not in words and "홍길동" not in words


def test_number_not_in_recording_is_missing(aligned):
    paras, _ = aligned
    p = paras["총 사업비는 3억 원으로 확정"]
    assert p.missing == ["3억"]                      # 녹취는 "2억 5천만 원"
    assert p.candidates[0].ts == "00:00:00"          # 그래도 사업비 논의 구간을 근거로 준다


def test_claim_without_evidence(aligned):
    paras, _ = aligned
    p = paras["자문단 회의는 분기마다 한 번"]
    assert p.candidates == [] and "자문단" in p.missing


def test_speaker_attribution_candidate(aligned):
    """메모의 발언자(홍길동)가 녹취에서 다른 사람을 가리켜도, 그 논의 구간을 근거 후보로 준다."""
    paras, _ = aligned
    c = paras["홍길동 위원: 조사 범위를 관광까지 넓히자고 함"].candidates[0]
    assert c.ts == "00:01:00" and "김철수 연구원이 낸 겁니다" in c.text
    assert "홍길동" in c.matched and "김철수?" in c.speakers   # tentative 이름은 ?를 붙여 보인다


def test_strong_match_on_numbers(aligned):
    paras, _ = aligned
    c = paras["설문조사는 4월 15일 시작"].candidates[0]
    assert c.ts == "00:02:00" and {"4월", "15일"} <= set(c.matched)


def test_uncovered_discussion(aligned):
    """메모가 다루지 않은 주차장 논의는 uncovered로 나온다."""
    _, unc = aligned
    assert len(unc) == 1 and unc[0]["ts"] == "00:03:20" and "주차장" in unc[0]["text"]
    assert unc[0]["text"].count("주차장 이용률") == 1   # 겹친 창을 이을 때 중복 없음


def test_docx_memo(tmp_path):
    p = tmp_path / "memo.docx"
    body = "".join(f"<w:p><w:r><w:t>{x}</w:t></w:r></w:p>" for x in ["설문조사는 4월 15일 시작", "중간보고는 6월"])
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="w"><w:body>{body}</w:body></w:document>')
    assert notes.memo_paragraphs(p) == ["설문조사는 4월 15일 시작", "중간보고는 6월"]


def test_cli_writes_align_and_to_series(tmp_path, monkeypatch):
    monkeypatch.setenv("REC2MD_SERIES_DIR", str(tmp_path / "series"))
    (tmp_path / "series" / "정례").mkdir(parents=True)
    seg = tmp_path / "meet.v2.segments.json"
    seg.write_text(SEGS.read_text(encoding="utf-8"), encoding="utf-8")
    cmd_notes.main([str(seg), str(MEMO), "--to-series", "정례"])
    data = json.loads((tmp_path / "meet.v2.notes-align.json").read_text(encoding="utf-8"))
    assert data["memo"] == "notes_memo.md" and len(data["paragraphs"]) == 10 and len(data["uncovered"]) == 1
    assert (tmp_path / "series" / "정례" / "docs" / "notes_memo.md").is_file()   # 시리즈 자료(출처 doc)


def test_cli_rejects_unknown_series(tmp_path, monkeypatch):
    monkeypatch.setenv("REC2MD_SERIES_DIR", str(tmp_path / "series"))
    with pytest.raises(SystemExit, match="시리즈 없음"):
        cmd_notes.main([str(SEGS), str(MEMO), "--to-series", "없음", "--out", str(tmp_path)])
    assert not list(tmp_path.glob("*.notes-align.json"))   # 시리즈가 없으면 아무것도 쓰지 않는다


def _transcript(texts: list[str], seg_sec: float = 10.0) -> Transcript:
    from rec2md.segments import Segment
    segs = [Segment(i * seg_sec, (i + 1) * seg_sec, x, "화자1") for i, x in enumerate(texts)]
    return Transcript("synthetic.wav", len(texts) * seg_sec, "test", "ko", segs, {})


def test_uncovered_keeps_tail_of_partially_covered_window():
    """10초 세그먼트 5개 → 창 [0,30], [20,50]. 메모가 [0,30]만 다뤄도 [30,50]의 빠진 논의는 남는다."""
    t = _transcript(["예산 총액 3억 원 확정", "예산 집행 일정 확인", "예산 항목 검토 마무리",
                     "주차장 협약서 문제 논의", "주차장 개인정보 처리 방안"])
    _, unc = notes.align(["예산 총액 3억 원 확정 집행 일정 항목 검토"], t)
    assert [(u["start"], u["end"]) for u in unc] == [(30.0, 50.0)] and "주차장" in unc[0]["text"]


def test_single_window_exact_match_has_candidate():
    """창이 하나뿐인 짧은 녹취에서도 정확히 겹치는 문단은 근거 후보가 생긴다."""
    t = _transcript(["주차장 이용률 데이터는 시청에서 받기로 함"])
    paras, _ = notes.align(["주차장 이용률 데이터"], t)
    assert paras[0].candidates and paras[0].candidates[0].ts == "00:00:00"
