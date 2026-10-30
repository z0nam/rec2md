"""render 모듈 단위 테스트: fmt_ts 및 raw transcript Markdown 렌더링."""

from rec2md.render import fmt_ts, render_raw
from rec2md.segments import Segment, Transcript


def test_fmt_ts():
    assert fmt_ts(0) == "00:00:00"
    assert fmt_ts(59.9) == "00:00:59"
    assert fmt_ts(65) == "00:01:05"
    assert fmt_ts(3665) == "01:01:05"


def test_render_raw_with_diarization():
    t = Transcript(
        source="meeting.m4a",
        duration=125.0,
        engine="local",
        language="ko",
        segments=[
            Segment(0.0, 5.0, "첫 번째 문장입니다.", speaker="화자1"),
            Segment(5.5, 10.0, "같은 화자의 두 번째 문장입니다.", speaker="화자1"),
            Segment(11.0, 15.0, "다른 화자의 응답입니다.", speaker="화자2"),
        ],
        meta={"diarizer": "pyannote-community-1"},
    )
    md = render_raw(t, title="테스트 회의 녹취록")

    assert "# 테스트 회의 녹취록" in md
    assert "- 원본: `meeting.m4a`" in md
    assert "- 길이: 00:02:05" in md
    assert "- 엔진: local / 화자분리: pyannote-community-1" in md
    assert "- 화자: 2명 (화자1, 화자2)" in md

    # 같은 화자(화자1)의 연속 발화는 합쳐져서 한 줄에 출력
    assert "**[00:00:00] 화자1** 첫 번째 문장입니다. 같은 화자의 두 번째 문장입니다." in md
    # 다른 화자(화자2)는 새 문단
    assert "**[00:00:11] 화자2** 다른 화자의 응답입니다." in md


def test_render_raw_no_diarize():
    t = Transcript(
        source="voice_memo.mp3",
        duration=30.0,
        engine="local",
        language="ko",
        segments=[
            Segment(0.0, 4.0, "화자 정보가 없는 첫 발화입니다.", speaker=None),
            Segment(5.0, 9.0, "두 번째 발화입니다.", speaker=None),
        ],
        meta={},
    )
    md = render_raw(t, title="음성 메모")

    assert "# 음성 메모" in md
    assert "화자:" not in md
    # 화자 정보가 없으면 세그먼트마다 각각 타임스탬프 표시
    assert "**[00:00:00]** 화자 정보가 없는 첫 발화입니다." in md
    assert "**[00:00:05]** 두 번째 발화입니다." in md


def test_render_raw_skips_empty_text():
    t = Transcript(
        source="test.wav",
        duration=10.0,
        engine="local",
        language="ko",
        segments=[
            Segment(0.0, 2.0, "   ", speaker="화자1"),
            Segment(2.0, 4.0, "유효한 문장", speaker="화자1"),
        ],
    )
    md = render_raw(t, title="빈 텍스트 테스트")
    assert "**[00:00:02] 화자1** 유효한 문장" in md
    assert "00:00:00" not in md
