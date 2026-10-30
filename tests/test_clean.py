"""clean 모듈 단위 테스트: 디코딩 루프 반복 정리, 알려진 환각 제거."""

from rec2md.clean import collapse_ngram_loops, collapse_repeats, drop_known_hallucinations
from rec2md.segments import Segment, Word


def test_collapse_repeats_under_or_equal_max_repeats():
    # 3번 이하 반복은 사람의 자연스러운 발화("네 네 네")로 보존
    words = [
        Word(0.0, 0.3, "네"),
        Word(0.4, 0.7, "네"),
        Word(0.8, 1.1, "네"),
    ]
    seg = Segment(0.0, 1.1, "네 네 네", words=words)
    res = collapse_repeats([seg])
    assert len(res) == 1
    assert len(res[0].words) == 3
    assert res[0].text == "네네네"


def test_collapse_repeats_exceeding_max_repeats():
    # 3번 초과 반복(디코딩 루프)은 3개까지만 남김
    words = [
        Word(0.0, 0.3, "초"),
        Word(0.4, 0.7, "초"),
        Word(0.8, 1.1, "초"),
        Word(1.2, 1.5, "초"),
        Word(1.6, 1.9, "초"),
    ]
    seg = Segment(0.0, 1.9, "초 초 초 초 초", words=words)
    res = collapse_repeats([seg])
    assert len(res) == 1
    assert len(res[0].words) == 3
    assert res[0].text == "초초초"
    assert res[0].start == 0.0
    assert res[0].end == 1.1


def test_collapse_repeats_across_segment_boundaries():
    # 세그먼트 경계를 넘어 이어지는 반복(간격 <= 1.0초)도 합산하여 처리
    s1 = Segment(0.0, 0.7, "네 네", words=[Word(0.0, 0.3, "네"), Word(0.4, 0.7, "네")])
    s2 = Segment(0.9, 1.6, "네 네", words=[Word(0.9, 1.2, "네"), Word(1.3, 1.6, "네")])

    res = collapse_repeats([s1, s2])
    # s1에 2개, s2에서 첫 1개만 남고 4번째 '네'는 잘려야 함
    assert len(res) == 2
    assert len(res[0].words) == 2
    assert len(res[1].words) == 1
    assert res[1].words[0].start == 0.9
    assert res[1].words[0].end == 1.2


def test_collapse_repeats_reset_on_large_gap():
    # 간격이 LOOP_GAP(1.0s)보다 크면 새 발화로 보아 카운트 리셋
    s1 = Segment(0.0, 0.7, "네 네", words=[Word(0.0, 0.3, "네"), Word(0.4, 0.7, "네")])
    s2 = Segment(3.0, 3.7, "네 네", words=[Word(3.0, 3.3, "네"), Word(3.4, 3.7, "네")])

    res = collapse_repeats([s1, s2])
    assert len(res) == 2
    assert len(res[0].words) == 2
    assert len(res[1].words) == 2


def test_collapse_repeats_empty_words():
    # words가 비어있는 세그먼트는 그대로 통과
    seg = Segment(0.0, 1.0, "말씀하세요", words=[])
    res = collapse_repeats([seg])
    assert len(res) == 1
    assert res[0].text == "말씀하세요"


def test_drop_known_hallucinations():
    segs = [
        Segment(0.0, 2.0, "시청해 주셔서 감사합니다."),
        Segment(2.0, 4.0, "구독과 좋아요 부탁드립니다."),
        Segment(4.0, 8.0, "오늘 회의 안건에 대해 말씀드리겠습니다."),
        Segment(8.0, 10.0, "MBC 뉴스였습니다."),
    ]
    res = drop_known_hallucinations(segs)
    assert len(res) == 1
    assert res[0].text == "오늘 회의 안건에 대해 말씀드리겠습니다."


def test_drop_known_hallucinations_long_text_kept():
    # 20자 초과 긴 문맥에 우연히 포함된 단어는 버리지 않음
    long_text = "이번 프로젝트 결과물에 대해 시청해주셔서감사합니다 라고 말하는 것은 적절하지 않습니다."
    seg = Segment(0.0, 5.0, long_text)
    res = drop_known_hallucinations([seg])
    assert len(res) == 1
    assert res[0].text == long_text


def _seg(tokens: list[str]) -> Segment:
    words = [Word(i * 0.3, i * 0.3 + 0.25, f" {t}") for i, t in enumerate(tokens)]
    return Segment(0.0, words[-1].end, "".join(w.text for w in words).strip(), words=words)


def test_collapse_ngram_loops_two_token_loop():
    # 두 토큰이 번갈아 도는 디코딩 루프는 collapse_repeats가 못 잡는다
    seg = _seg(["알겠습니다."] + ["가나", "bar,"] * 20)
    assert collapse_repeats([seg])[0].text.count("bar") == 20
    res = collapse_ngram_loops([_seg(["알겠습니다."] + ["가나", "bar,"] * 20)])
    assert res[0].text == "알겠습니다. 가나 bar, 가나 bar, 가나 bar,"
    assert res[0].end == res[0].words[-1].end


def test_collapse_ngram_loops_keeps_natural_repeats():
    # 3번까지의 반복과 섞인 반복은 그대로 둔다
    tokens = ["좋아요", "좋아요", "그래서", "예산", "그래서", "예산", "그래서", "예산", "확정"]
    res = collapse_ngram_loops([_seg(tokens)])
    assert len(res[0].words) == len(tokens)


def test_collapse_ngram_loops_three_token_loop():
    res = collapse_ngram_loops([_seg(["다", "나", "가"] * 6)])
    assert [w.text.strip() for w in res[0].words] == ["다", "나", "가"] * 3


def test_collapse_ngram_loops_resets_across_pauses():
    # 4초 간격으로 네 번 말한 "확인 완료."는 루프가 아니라 각각의 발화다
    words = []
    for k in range(4):
        words += [Word(k * 4.0, k * 4.0 + 0.3, " 확인"), Word(k * 4.0 + 0.35, k * 4.0 + 0.7, " 완료.")]
    seg = Segment(0.0, words[-1].end, "".join(w.text for w in words).strip(), words=words)
    res = collapse_ngram_loops([seg])
    assert len(res[0].words) == 8 and res[0].end == words[-1].end
