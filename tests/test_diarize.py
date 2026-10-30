"""diarize 모듈 단위 테스트: speech_regions, _smooth, assign_speakers 등 화자 정렬 로직."""

from rec2md.diarize import (
    Turn,
    _overlap,
    _smooth,
    _speaker_at,
    assign_speakers,
    speech_regions,
)
from rec2md.segments import Segment, Word


def test_speech_regions_empty():
    assert speech_regions([]) == []


def test_speech_regions_merge_close_turns():
    # 2초 미만 간격의 구간들은 하나의 청크로 병합 (최대 25초 이내)
    turns: list[Turn] = [
        (0.0, 5.0, "spk1"),
        (6.0, 10.0, "spk2"),  # 간격 1초 -> 병합 (0~10초, 총 10초 <= 25초)
        (11.5, 15.0, "spk1"), # 간격 1.5초 -> 병합 (0~15초, 총 15초 <= 25초)
    ]
    regions = speech_regions(turns)
    assert len(regions) == 1
    assert regions[0] == (0.0, 15.0)


def test_speech_regions_split_on_large_gap():
    # 2초 이상 간격은 분리
    turns: list[Turn] = [
        (0.0, 5.0, "spk1"),
        (8.0, 12.0, "spk2"),  # 간격 3초 -> 분리
    ]
    regions = speech_regions(turns)
    assert len(regions) == 2
    assert regions[0] == (0.0, 5.0)
    assert regions[1] == (8.0, 12.0)


def test_speech_regions_split_when_exceeding_max_len():
    # 병합 시 총 길이가 25초를 넘어가면 분리
    turns: list[Turn] = [
        (0.0, 20.0, "spk1"),
        (21.0, 27.0, "spk2"),  # 0~27초 = 27초 > 25초 -> 분리
    ]
    regions = speech_regions(turns)
    assert len(regions) == 2
    assert regions[0] == (0.0, 20.0)
    assert regions[1] == (21.0, 27.0)


def test_speech_regions_overlapping_turns():
    # 겹침 발화인데 길이 한도를 넘을 때의 처리
    turns: list[Turn] = [
        (0.0, 24.0, "spk1"),
        (23.0, 30.0, "spk2"),  # 겹침 발화이지만 0~30초는 30초 > 25초
    ]
    regions = speech_regions(turns)
    assert len(regions) == 2
    assert regions[0] == (0.0, 24.0)
    assert regions[1] == (24.0, 30.0)


def test_overlap_and_speaker_at():
    turns: list[Turn] = [
        (0.0, 3.0, "SPEAKER_00"),
        (4.0, 8.0, "SPEAKER_01"),
    ]
    # 구간과 완전히 겹치는 단어
    w1 = Word(1.0, 2.0, "안녕")
    assert _overlap(w1, turns) == 1.0
    assert _speaker_at(w1, turns) == "SPEAKER_00"

    # 구간 사이에 걸친 단어 -> 겹침 없음(<= 0), 가장 가까운 구간 화자 매칭
    w_between = Word(3.2, 3.6, "중간")
    assert _overlap(w_between, turns) <= 0.0
    assert _speaker_at(w_between, turns) == "SPEAKER_00"


def test_smooth_sandwiched_short_run():
    # A - B - A 구조에서 B가 짧고(<=2 단어, <=1.0초) 양쪽과 0.3초 이내로 붙어있으면 A로 스무딩
    words = [
        Word(0.0, 1.0, "앞발화", speaker="화자1"),
        Word(1.1, 1.3, "음", speaker="화자2"),  # 1단어, 0.2초, 간격 0.1s / 0.1s
        Word(1.4, 2.5, "뒷발화", speaker="화자1"),
    ]
    _smooth(words)
    assert words[1].speaker == "화자1"


def test_smooth_not_sandwiched():
    # A - B - C 구조에서는 B가 짧아도 스무딩하지 않음 (진짜 다른 화자의 발화일 수 있음)
    words = [
        Word(0.0, 1.0, "앞발화", speaker="화자1"),
        Word(1.1, 1.3, "네", speaker="화자2"),
        Word(1.4, 2.5, "뒷발화", speaker="화자3"),
    ]
    _smooth(words)
    assert words[1].speaker == "화자2"


def test_smooth_not_glued():
    # 간격이 0.3초 이상 벌어져 있으면 스무딩하지 않음
    words = [
        Word(0.0, 1.0, "앞발화", speaker="화자1"),
        Word(1.5, 1.8, "네", speaker="화자2"),  # 1.5 - 1.0 = 0.5s > 0.3s
        Word(2.0, 3.0, "뒷발화", speaker="화자1"),
    ]
    _smooth(words)
    assert words[1].speaker == "화자2"


def test_assign_speakers():
    turns: list[Turn] = [
        (0.0, 2.0, "SPEAKER_A"),
        (2.5, 5.0, "SPEAKER_B"),
    ]
    segs = [
        Segment(
            0.0,
            5.0,
            "안녕하세요 네 반갑습니다",
            words=[
                Word(0.1, 0.9, "안녕하세요", prob=0.9),
                Word(2.6, 3.2, "네", prob=0.95),
                Word(3.3, 4.5, "반갑습니다", prob=0.9),
            ],
        )
    ]
    assigned = assign_speakers(segs, turns)

    # 화자가 바뀌었으므로 2개의 세그먼트로 분리되어야 함
    assert len(assigned) == 2

    # 첫 등장 순서대로 SPEAKER_A -> 화자1, SPEAKER_B -> 화자2
    assert assigned[0].speaker == "화자1"
    assert assigned[0].text == "안녕하세요"
    assert assigned[0].start == 0.1
    assert assigned[0].end == 0.9

    assert assigned[1].speaker == "화자2"
    assert assigned[1].text == "네반갑습니다"
    assert assigned[1].start == 2.6
    assert assigned[1].end == 4.5
