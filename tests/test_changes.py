"""rec2md changes: review 편집 이후 바뀐 곳만 뽑기(#56). 합성 데이터(가짜 이름)만 쓴다."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from rec2md import cmd_changes
from rec2md.changes import changes, parse_since, render_text
from rec2md.review import Session
from rec2md.segments import Segment, Transcript

LABELS = ["화자1", "화자2", "화자1", "화자2", "화자1", "화자3"]
T0, T1, T2 = "2026-11-18T10:00:00+09:00", "2026-11-18T11:00:00+09:00", "2026-11-18T12:00:00+09:00"


@pytest.fixture
def seg(tmp_path) -> Path:
    p = tmp_path / "meet.segments.json"
    segs = [Segment(i * 10.0, i * 10.0 + 9, f"문장{i}", lb) for i, lb in enumerate(LABELS)]
    Transcript("x.wav", 60.0, "test", "ko", segs, {"speaker_names": {}}).to_json(p)
    return p


def at(t: str) -> datetime:
    return datetime.fromisoformat(t)


def set_mtime(p: Path, t: str) -> None:
    ts = at(t).timestamp()
    os.utime(p, (ts, ts))


def name_all(seg_i, frm, name, when):
    return {"op": "set_speaker", "scope": "all", "seg": seg_i, "from": frm, "to": {"name": name}, "at": when}


def text(seg_i, frm, to, when):
    return {"op": "set_text", "seg": seg_i, "from": frm, "to": to, "at": when}


def test_review_records_edit_time(seg):
    s = Session(seg)
    s.add({"op": "set_text", "seg": 0, "from": "문장0", "to": "고친 문장"})
    log = json.loads(seg.with_name("meet.edits.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(log[0]["at"])  # 편집마다 시각이 남는다


def test_only_changes_after_since(seg):
    s = Session(seg)
    s.add(text(0, "문장0", "예전에 고친 문장", T0))
    s.add(name_all(0, "화자1", "홍길동", T2))
    s.add(text(3, "문장3", "새로 고친 문장", T2))
    c = changes(seg, at(T1))
    assert c.edits_total == 3 and c.edits_after == 2
    assert [t["seg"] for t in c.texts] == [3]
    assert c.texts[0]["text_before"] == "문장3" and c.texts[0]["text_after"] == "새로 고친 문장"
    # 화자 이름 전체 변경은 구간마다 늘어놓지 않고 한 줄로
    assert c.speakers == [{"from": "화자1", "to": "홍길동", "n": 3, "all": True, "first_ts": "00:00:00",
                           "segs": [0, 2, 4]}]
    out = render_text(c, seg)
    assert "화자1 → 홍길동 (전체 3구간)" in out
    assert "예전에 고친 문장" not in out  # 기준 시점 이전 편집은 안 보인다
    assert "[00:00:30] 화자2" in out and "후: 새로 고친 문장" in out


def test_text_change_shows_speaker_before_after(seg):
    s = Session(seg)
    s.add({"op": "set_speaker", "scope": "one", "seg": 1, "from": "화자2", "to": "화자1", "at": T2})
    s.add(text(1, "문장1", "고침", T2))
    c = changes(seg, at(T1))
    assert c.texts[0]["speaker_before"] == "화자2" and c.texts[0]["speaker_after"] == "화자1"
    assert c.speakers == []  # 문장도 바뀐 구간은 문장 쪽에만
    assert "화자2 → 화자1" in render_text(c, seg)


def test_partial_speaker_change_is_not_all(seg):
    Session(seg).add({"op": "set_speaker", "scope": "after", "seg": 2, "from": "화자1", "to": "화자3", "at": T2})
    g, = changes(seg, at(T1)).speakers
    assert (g["from"], g["to"], g["n"], g["all"]) == ("화자1", "화자3", 2, False)


def test_default_since_is_transcript_mtime_and_none_means_all(seg):
    assert parse_since(None, seg) is None  # 녹취록.md가 없으면 전체
    md = seg.with_name("meet.녹취록.md")
    md.write_text("# 녹취록\n", encoding="utf-8")
    set_mtime(md, T1)
    assert parse_since(None, seg) == at(T1)
    assert parse_since(str(md), seg) == at(T1)
    assert parse_since("2026-11-18T11:00:00+09:00", seg) == at(T1)
    with pytest.raises(SystemExit):
        parse_since("어제", seg)


def test_edits_without_time_are_all_included(seg):
    """예전 로그(at 없음)는 시각 불명으로 기준 시점과 상관없이 포함한다."""
    s = Session(seg)
    s.add(text(0, "문장0", "옛 편집", T0))
    s.add(text(1, "문장1", "새 편집", T2))
    p = seg.with_name("meet.edits.json")
    log = json.loads(p.read_text(encoding="utf-8"))
    for e in log:
        del e["at"]
    p.write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    c = changes(seg, at(T1))
    assert c.unknown_time == 2 and [t["seg"] for t in c.texts] == [0, 1]
    assert "시각 불명 2건" in render_text(c, seg)


def test_memo_change_diff_and_seen_copy(seg):
    memo = seg.with_name("meet.memo.md")
    memo.write_text("첫 메모\n", encoding="utf-8")
    set_mtime(memo, T0)
    assert changes(seg, at(T1)).memo is None  # 기준 시점 전 메모는 안 보인다
    m = changes(seg, None).memo
    assert "+첫 메모" in m["diff"]
    memo.write_text("첫 메모\n0:00:30 문단 고칠 것\n", encoding="utf-8")
    set_mtime(memo, T2)
    m = changes(seg, at(T1)).memo
    assert "+0:00:30 문단 고칠 것" in m["diff"] and "+첫 메모" not in m["diff"]  # 지난번 본 메모와의 차이만
    assert seg.with_name("meet.memo.seen.md").read_text(encoding="utf-8") == memo.read_text(encoding="utf-8")


def test_no_changes(seg):
    c = changes(seg, None)
    assert c.count() == 0 and "바뀐 곳 없음" in render_text(c, seg)


def test_cli_json(seg, capsys):
    Session(seg).add(name_all(0, "화자1", "홍길동", T2))
    cmd_changes.main([str(seg), "--since", T1, "--json"])
    d = json.loads(capsys.readouterr().out)
    assert d["count"] == 1 and d["speakers"][0]["to"] == "홍길동"


def test_panel_shows_change_count_for_handwritten_transcript(seg):
    md = seg.with_name("meet.녹취록.md")
    md.write_text("# 녹취록\n", encoding="utf-8")
    set_mtime(md, T1)
    s = Session(seg)
    s.add(text(0, "문장0", "고침", T2))
    s.add(name_all(1, "화자2", "홍길동", T2))
    sm = s.panel()["summary"]
    assert sm["stale"] and sm["changes"]["count"] == 2
    assert sm["changes"]["cmd"] == f"rec2md changes {seg}"
