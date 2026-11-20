"""전사 누락 의심 구간(#58): 합성 세그먼트만 쓴다."""

from __future__ import annotations

from rec2md import gaps, speechcheck
from rec2md.render import render_raw
from rec2md.segments import Segment, Transcript

LINE = "가나다라마바사아자차카타파하" * 2  # 공백 없이 28자


def _meeting(sparse: tuple[float, float] | None = (600.0, 1000.0), meta: dict | None = None) -> Transcript:
    """화자A·B가 20초씩 번갈아 말하는 회의(약 1.4자/초). sparse 구간은 화자C가 말하는데 글이 거의 없다."""
    segs, t, k = [], 0.0, 0
    while t < 1500:
        if sparse and sparse[0] <= t < sparse[1]:
            segs.append(Segment(sparse[0], sparse[0] + 20, LINE, "화자C"))
            segs.append(Segment(sparse[0] + 20, sparse[1], "네", "화자C"))  # 앞 조각만 남고 비었다
            t = sparse[1]
            continue
        segs.append(Segment(t, t + 20, LINE, "화자A" if k % 2 else "화자B"))
        t, k = t + 20, k + 1
    return Transcript("x.wav", 1500.0, "test", "ko", segs, meta or {})


def test_flags_long_sparse_block():
    (g,) = gaps.suspects(_meeting())
    assert (g["start"], g["end"]) == (600.0, 1000.0)
    assert g["cps"] < 0.1 and g["median"] == 1.4 and g["trim_cut"] == []


def test_normal_meeting_has_no_suspects():
    assert gaps.suspects(_meeting(sparse=None)) == []


def test_short_quiet_block_is_not_flagged():
    """2분 미만 문단은 밀도가 낮아도(맞장구·침묵) 표식하지 않는다. 단 세그먼트 하나가 1분을 넘으면 그 자체로 표식."""
    assert gaps.suspects(_meeting(sparse=(600.0, 660.0))) == []
    (g,) = gaps.suspects(_meeting(sparse=(600.0, 700.0)))  # 80초짜리 세그먼트
    assert (g["start"], g["end"]) == (620.0, 700.0)


def test_too_few_blocks_no_median():
    t = Transcript("x.wav", 400.0, "test", "ko", [Segment(0, 300, "네", "화자A")], {})
    assert gaps.suspects(t) == []


def test_trim_cut_overlap_is_reported():
    t = _meeting(meta={"trim": {"cut": [[640.0, 980.0]]}})
    (g,) = gaps.suspects(t)
    assert g["trim_cut"] == [[640.0, 980.0]]
    assert gaps.note(g).startswith("원인: trim으로 잘림(00:10:40-00:16:20") and "--no-trim" in gaps.note(g)
    assert gaps.note(gaps.suspects(_meeting())[0]).startswith("원인 미상")


def test_raw_md_marks_gap_in_place():
    md = render_raw(_meeting(), "회의")
    head, body = md.split("---", 1)
    assert "⚠ 전사 누락 의심 1곳" in head and "00:10:00-00:16:40" in head
    lines = body.splitlines()
    i = next(n for n, x in enumerate(lines) if x.startswith("> ⚠ 전사 누락 의심 [00:10:00-00:16:40]"))
    assert lines[i + 2].startswith("**[00:10:00] 화자C**")  # 표식 바로 뒤가 그 문단


def test_raw_md_unchanged_without_gaps():
    assert "⚠" not in render_raw(_meeting(sparse=None), "회의")


def test_cut_checker_none_without_vad(monkeypatch):
    monkeypatch.setattr(speechcheck, "_load_vad", lambda: None)
    assert speechcheck.cut_checker(None) is None
