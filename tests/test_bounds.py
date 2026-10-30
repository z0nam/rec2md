"""회의 구간 추정. 합성 세그먼트만 쓴다."""

from __future__ import annotations

from rec2md.bounds import estimate, summary
from rec2md.cmd_bounds import main as bounds_main
from rec2md.segments import Segment, Transcript


def meeting(start: float, end: float, speakers=("화자1", "화자2", "화자3", "화자4"), turn: float = 20.0):
    """여러 사람이 빈틈없이 돌아가며 말하는 구간."""
    out, t, i = [], start, 0
    while t + turn <= end:
        out.append(Segment(t, t + turn - 0.5, "예산 배분 기준을 다시 검토해야 할 것 같습니다", speaker=speakers[i % len(speakers)]))
        t += turn
        i += 1
    return out


def chatter(start: float, end: float, speaker: str = "화자1", every: float = 40.0):
    """한 사람이 띄엄띄엄 말하는 구간(통화·혼잣말)."""
    out, t = [], start
    while t + 8 <= end:
        out.append(Segment(t, t + 8, "네 네 알겠습니다 그럼 그렇게 할게요", speaker=speaker))
        t += every
    return out


def test_tail_after_closing_is_cut():
    segs = meeting(0, 1800) + [Segment(1800, 1806, "이상으로 오늘 회의를 마치겠습니다 수고하셨습니다", speaker="화자1")]
    segs += chatter(1830, 2400, speaker="화자2")
    b = estimate(Transcript("x", 2400, "local", "ko", segs))
    assert abs(b["end"] - 1806) < 1
    assert b["start"] == 0
    assert b["confidence"] == "high"
    assert any("종료 표현" in e for e in b["evidence"])


def test_whole_meeting_is_not_cut():
    segs = meeting(0, 2400) + [Segment(2400, 2405, "수고하셨습니다", speaker="화자2")]
    b = estimate(Transcript("x", 2410, "local", "ko", segs))
    assert b["confidence"] == "none" and b["end"] == 2410 and b["start"] == 0


def test_closing_greeting_mid_meeting_alone_is_not_enough():
    # 회의 중간의 인사("수고하셨습니다")만으로는 자르지 않는다
    segs = meeting(0, 1500) + [Segment(1500, 1504, "발표 수고하셨습니다", speaker="화자3")] + meeting(1505, 2700)
    b = estimate(Transcript("x", 2700, "local", "ko", segs))
    assert b["end"] == 2700


def test_pre_meeting_chatter_start():
    segs = chatter(0, 480, speaker="화자1", every=60)
    segs += [Segment(500, 505, "그럼 회의 시작하겠습니다", speaker="화자2")] + meeting(506, 2600)
    b = estimate(Transcript("x", 2600, "local", "ko", segs))
    assert 480 <= b["start"] <= 506
    assert b["end"] == 2600


def test_short_recording_skipped():
    b = estimate(Transcript("x", 300, "local", "ko", meeting(0, 300)))
    assert b["confidence"] == "none" and "짧아" in b["evidence"][0]


def test_summary_and_cli_write(tmp_path, capsys):
    segs = meeting(0, 1800) + [Segment(1800, 1806, "이상으로 회의를 마치겠습니다", speaker="화자1")]
    segs += chatter(1830, 2400, speaker="화자2")
    t = Transcript("x.wav", 2400, "local", "ko", segs)
    assert "뒤 00:09:54" in summary(estimate(t), 2400)
    p = tmp_path / "x.segments.json"
    t.to_json(p)
    bounds_main([str(p), "--write"])
    assert "제외 후보" in capsys.readouterr().out
    assert Transcript.from_json(p).meta["meeting_bounds"]["end"] == 1806
