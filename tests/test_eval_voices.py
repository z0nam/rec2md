"""scripts/eval_voices.py의 판정 반영·구간 자르기 (합성 데이터)."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import eval_voices as ev  # noqa: E402

from rec2md.segments import Segment, Transcript  # noqa: E402


def _rec(turns, clova_segs, gold):
    clova = Transcript(source="", duration=100.0, engine="clova", language="ko", segments=clova_segs)
    secs = {}
    for s, e, k in turns:
        secs[k] = secs.get(k, 0) + e - s
    return ev.Rec("R1", Path("a.wav"), clova, turns, {k: np.ones(4) for k in secs}, {}, secs, gold)


def test_purity_marks_merged_clova_speaker_impure():
    # 클로바가 홍길동·김철수를 한 화자 "홍길동"으로 묶었다 → 우리 S1(김철수)은 홍길동 라벨과 겹치지만 불순
    turns = [(0, 40, "S0"), (40, 70, "S1"), (70, 100, "S2")]
    clova = [Segment(0, 70, "", "홍길동"), Segment(70, 100, "", "이영희")]
    rec = _rec(turns, clova, {"홍길동": ("홍길동", "clova-manual"), "이영희": ("이영희", "clova-manual")})
    ev.map_clova(rec)
    assert rec.truth["S2"] == ("이영희", "clova-manual")
    assert "S0" in rec.impure  # 홍길동 라벨 시간의 57%만 S0 → 순도 미달


def test_answers_override_and_split():
    turns = [(0, 50, "S0"), (50, 100, "S1")]
    rec = _rec(turns, [Segment(0, 100, "", "홍길동")], {"홍길동": ("홍길동", "clova-manual")})
    ev.map_clova(rec)
    opts = [{"key": "1", "label": "홍길동"}, {"key": "2", "label": "김철수"}, {"key": "3", "label": ev.NONE_OF_THEM}]
    cases = [{"id": f"A-00{i}", "options": opts, "meta": {"cluster": c}}
             for i, c in enumerate(["S0", "S0", "S0", "S1", "S1"])]
    answers = [{"id": "A-000", "key": "1"}, {"id": "A-001", "key": "1"}, {"id": "A-002", "key": None, "unsure": True},
               {"id": "A-003", "key": "2"}, {"id": "A-004", "key": "1"}]
    assert ev.apply_answers(rec, cases, answers) == 2
    assert rec.truth == {"S0": ("홍길동", "user")}
    assert "갈림" in rec.impure["S1"]


def test_clip_length():
    assert ev._clip(10, 12) is None
    assert ev._clip(10, 14) == (10, 14)
    s, e = ev._clip(0, 30)
    assert e - s == 8 and s == 11


def test_bounds_and_non_names():
    assert ev._parse_bounds(["a b=0-49:40"]) == {"a b": (0.0, 2980.0)}
    assert ev._parse_bounds(["x=1:00-"])["x"] == (60.0, float("inf"))
    assert ev._clip_bounds([(0, 10, "S0"), (2970, 2990, "S1"), (3000, 3010, "S2")], 0, 2980) == \
        [(0, 10, "S0"), (2970, 2980, "S1")]
    for label in ["미지정 1", "○○기관 담당자님 (성함확인필요)", "Attendees 3", "00:00 ~"]:
        assert ev.NOT_A_NAME.search(label)
    assert not ev.NOT_A_NAME.search("홍길동")
