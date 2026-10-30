"""클로바식 녹취록 내보내기(txt, md, html) 및 round-trip 테스트."""

from pathlib import Path
import pytest

from rec2md import clova_txt
from rec2md.segments import Segment, Transcript
from rec2md.transcript_export import (
    export_transcript,
    format_duration,
    format_ts,
    render_clova_html,
    render_clova_md,
    render_clova_txt,
    resolve_speaker,
)


@pytest.fixture
def synthetic_transcript() -> Transcript:
    segs = [
        Segment(start=0.0, end=15.0, text="안녕하세요. 오늘 안건 회의를 시작하겠습니다.", speaker="화자1"),
        Segment(start=15.0, end=45.0, text="네 반갑습니다. 바로 1번 안건 보고해 주세요.", speaker="화자2"),
        Segment(start=45.0, end=3665.0, text="1번 안건은 시스템 성능 최적화 건입니다.", speaker="화자1"),
        Segment(start=3665.0, end=3720.0, text="1시간이 지났으니 마무리 발언 부탁드립니다.", speaker="화자3"),
    ]
    return Transcript(
        source="sample_meeting.wav",
        duration=3720.0,
        engine="mock-engine",
        language="ko",
        segments=segs,
        meta={
            "title": "합성 테스트 회의",
            "date": "2026.10.30",
            "time": "10:00",
        },
    )


def test_format_duration():
    assert format_duration(45.0) == "0Minutes 45seconds"
    assert format_duration(75.0) == "1Minutes 15seconds"
    assert format_duration(3665.0) == "1Hours 1Minutes 5seconds"
    assert format_duration(4497.0) == "1Hours 14Minutes 57seconds"
    assert format_duration(0.0) == "0Minutes 0seconds"


def test_format_ts():
    assert format_ts(0.0) == "00:00"
    assert format_ts(65.0) == "01:05"
    assert format_ts(605.0) == "10:05"
    assert format_ts(3600.0) == "01:00:00"
    assert format_ts(3723.0) == "01:02:03"


def test_resolve_speaker():
    meta = {
        "speaker_names": {
            "화자1": {"name": "홍길동", "score": 0.9},
            "화자2": "김철수",
        }
    }
    # 1. names dict 인자 우선
    assert resolve_speaker("화자1", {"화자1": "이영희"}, meta) == "이영희"
    # 2. meta.speaker_names (dict 형태)
    assert resolve_speaker("화자1", None, meta) == "홍길동"
    # 3. meta.speaker_names (str 형태)
    assert resolve_speaker("화자2", None, meta) == "김철수"
    # 4. 미등록 화자 라벨 그대로 유지
    assert resolve_speaker("화자3", None, meta) == "화자3"
    # 5. 빈 값 폴백
    assert resolve_speaker(None, None, {}) == "화자"


def test_export_txt_roundtrip(synthetic_transcript, tmp_path):
    names = {"화자1": "홍길동", "화자2": "김철수", "화자3": "이영희"}
    out_file = tmp_path / "meeting.txt"

    res = export_transcript(synthetic_transcript, "txt", out_file, names=names)
    assert res == out_file
    assert out_file.exists()

    # clova_txt.parse 로 다시 읽었을 때 일치하는지 확인
    parsed = clova_txt.parse(out_file)
    assert parsed.meta["title"] == synthetic_transcript.meta["title"]
    assert parsed.duration == synthetic_transcript.duration
    assert len(parsed.segments) == len(synthetic_transcript.segments)

    expected_speakers = ["홍길동", "김철수", "홍길동", "이영희"]
    for orig, back, exp_spk in zip(synthetic_transcript.segments, parsed.segments, expected_speakers):
        assert back.speaker == exp_spk
        assert back.start == orig.start
        assert back.end == orig.end
        assert back.text == orig.text


def test_export_md(synthetic_transcript, tmp_path):
    out_file = tmp_path / "meeting.md"
    export_transcript(synthetic_transcript, "md", out_file, names={"화자1": "홍길동"})

    content = out_file.read_text(encoding="utf-8")
    assert "# 합성 테스트 회의" in content
    assert "홍길동" in content
    assert "**홍길동** `00:00`" in content
    assert "안녕하세요. 오늘 안건 회의를 시작하겠습니다." in content


def test_export_html(synthetic_transcript, tmp_path):
    out_file = tmp_path / "meeting.html"
    export_transcript(synthetic_transcript, "html", out_file)

    content = out_file.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "<title>합성 테스트 회의</title>" in content
    assert "class=\"speaker-name\"" in content
    assert "@media print" in content


def test_export_to_directory(synthetic_transcript, tmp_path):
    out_dir = tmp_path / "output_dir"
    out_dir.mkdir()

    res = export_transcript(synthetic_transcript, "txt", out_dir)
    assert res == out_dir / "sample_meeting.txt"
    assert res.exists()


def test_invalid_format(synthetic_transcript, tmp_path):
    with pytest.raises(ValueError, match="지원하지 않는 형식"):
        export_transcript(synthetic_transcript, "pdf", tmp_path / "meeting.pdf")


def test_format_variations_and_empty_segments(tmp_path):
    # 빈 세그먼트 및 메타데이터 최소화 케이스
    empty_t = Transcript(
        source="empty_sample.wav",
        duration=0.0,
        engine="test",
        language="ko",
        segments=[],
        meta={},
    )
    txt_out = tmp_path / "empty.txt"
    export_transcript(empty_t, ".TXT", txt_out)
    assert txt_out.exists()
    parsed = clova_txt.parse(txt_out)
    assert parsed.duration == 0.0
    assert len(parsed.segments) == 0

    # 공백 텍스트만 있는 세그먼트는 내보내기에서 스킵되는지 확인
    ws_t = Transcript(
        source="ws_sample.wav",
        duration=10.0,
        engine="test",
        language="ko",
        segments=[
            Segment(start=0.0, end=5.0, text="   ", speaker="화자1"),
            Segment(start=5.0, end=10.0, text="유효 발화", speaker="화자1"),
        ],
        meta={},
    )
    ws_out = tmp_path / "ws.md"
    export_transcript(ws_t, "MD", ws_out)
    content = ws_out.read_text(encoding="utf-8")
    assert "유효 발화" in content
    assert content.count("**화자1**") == 1


def test_local_clova_roundtrip_if_available(tmp_path):
    """local/clova 폴더가 로컬에 있는 경우 실제 샘플 텍스트와의 roundtrip 테스트."""
    local_clova = Path("local/clova")
    if not local_clova.is_dir():
        pytest.skip("local/clova 샘플 없음")

    sample_files = list(local_clova.glob("*.txt"))
    if not sample_files:
        pytest.skip("local/clova 내 txt 파일 없음")

    for p in sample_files:
        orig_transcript = clova_txt.parse(p)
        out_txt = tmp_path / f"export_{p.name}"
        export_transcript(orig_transcript, "txt", out_txt)

        reloaded = clova_txt.parse(out_txt)
        assert reloaded.duration == orig_transcript.duration, f"{p.name} duration mismatch"
        assert reloaded.meta["title"] == orig_transcript.meta["title"], f"{p.name} title mismatch"
        assert len(reloaded.segments) == len(orig_transcript.segments), f"{p.name} segments len mismatch"

        for i, (s1, s2) in enumerate(zip(orig_transcript.segments, reloaded.segments)):
            assert s1.speaker == s2.speaker, f"{p.name}[{i}] speaker mismatch"
            assert s1.start == s2.start, f"{p.name}[{i}] start mismatch"
            assert s1.end == s2.end, f"{p.name}[{i}] end mismatch"
            assert s1.text == s2.text, f"{p.name}[{i}] text mismatch"
