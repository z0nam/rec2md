"""클로바식 녹취록 내보내기(txt, md, html) 및 round-trip 테스트."""

from pathlib import Path
import pytest

from rec2md import clova_txt
from rec2md.segments import Segment, Transcript
from rec2md.transcript_export import (
    export_transcript,
    format_duration,
    format_ts,
    iter_turns,
    render_clova_html,
    render_clova_md,
    render_clova_txt,
    resolve_speaker,
)


@pytest.fixture
def synthetic_transcript() -> Transcript:
    segs = [
        Segment(start=0.0, end=10.0, text="안녕하세요.", speaker="화자1"),
        Segment(start=10.0, end=15.0, text="오늘 안건 회의를 시작하겠습니다.", speaker="화자1"),
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
    # 1시간 이상은 H:MM:SS
    assert format_ts(3600.0) == "1:00:00"
    assert format_ts(3723.0) == "1:02:03"
    assert format_ts(7325.0) == "2:02:05"


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
    # 4. 화자N -> 참석자 N 변환
    assert resolve_speaker("화자3", None, meta) == "참석자 3"
    assert resolve_speaker("SPEAKER_00", None, meta) == "참석자 1"
    # 5. 빈 값 폴백
    assert resolve_speaker(None, None, {}) == "참석자 1"


def test_iter_turns_merges_consecutive_same_speaker(synthetic_transcript):
    # synthetic_transcript의 첫 두 세그먼트("화자1")는 하나로 병합되어야 함
    turns = iter_turns(synthetic_transcript)
    assert len(turns) == 4
    assert turns[0].speaker == "참석자 1"
    assert turns[0].start == 0.0
    assert turns[0].text == "안녕하세요. 오늘 안건 회의를 시작하겠습니다."
    assert turns[1].speaker == "참석자 2"
    assert turns[2].speaker == "참석자 1"
    assert turns[3].speaker == "참석자 3"


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

    # 병합된 turn 기준으로 4개 세그먼트 생성
    expected = [
        ("홍길동", 0.0, 15.0, "안녕하세요. 오늘 안건 회의를 시작하겠습니다."),
        ("김철수", 15.0, 45.0, "네 반갑습니다. 바로 1번 안건 보고해 주세요."),
        ("홍길동", 45.0, 3665.0, "1번 안건은 시스템 성능 최적화 건입니다."),
        ("이영희", 3665.0, 3720.0, "1시간이 지났으니 마무리 발언 부탁드립니다."),
    ]
    assert len(parsed.segments) == len(expected)
    for (exp_spk, exp_start, exp_end, exp_text), actual in zip(expected, parsed.segments):
        assert actual.speaker == exp_spk
        assert actual.start == exp_start
        assert actual.end == exp_end
        assert actual.text == exp_text


def test_export_custom_title_and_recorded_at(synthetic_transcript, tmp_path):
    out_file = tmp_path / "custom.txt"
    export_transcript(
        synthetic_transcript,
        "txt",
        out_file,
        title="사용자 지정 안건 회의",
        recorded_at="2026.11.01 Sun PM 2:00",
    )

    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "사용자 지정 안건 회의"
    assert lines[1].startswith("2026.11.01 Sun PM 2:00 ・")

    parsed = clova_txt.parse(out_file)
    assert parsed.meta["title"] == "사용자 지정 안건 회의"


def test_export_md(synthetic_transcript, tmp_path):
    out_file = tmp_path / "meeting.md"
    export_transcript(synthetic_transcript, "md", out_file, names={"화자1": "홍길동"})

    content = out_file.read_text(encoding="utf-8")
    assert "# 합성 테스트 회의" in content
    assert "홍길동" in content
    assert "**홍길동** 00:00" in content
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
    assert content.count("**참석자 1**") == 1
