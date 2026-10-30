"""맥락 보정(B): low_conf 보존, 시리즈 용어집, 프롬프트 반향 필터, 보정 후보. 합성 데이터만 쓴다."""

from __future__ import annotations

import json

from rec2md.clean import drop_known_hallucinations
from rec2md.cmd_series import main as series_main
from rec2md.corrections import edit_distance, find_candidates, jamo
from rec2md.segments import Segment, Transcript, Word, low_conf_spans
from rec2md.series import Context, build_prompt, extract_terms, load_series, parse_glossary


def test_low_conf_spans_merge_runs():
    words = [Word(0, 1, " 탐나", prob=0.3), Word(1, 2, "데이터", prob=0.4), Word(2, 3, " 센터", prob=0.9),
             Word(3, 4, " 홍길똥", prob=0.2)]
    assert low_conf_spans(words) == [[0, 2, 0.3, "탐나데이터"], [3, 4, 0.2, "홍길똥"]]


def test_to_json_keeps_low_conf_drops_words(tmp_path):
    seg = Segment(0, 2, "탐나 센터", words=[Word(0, 1, " 탐나", prob=0.3), Word(1, 2, " 센터", prob=0.9)])
    t = Transcript("x.wav", 2, "local", "ko", [seg, Segment(2, 3, "네")])
    p = tmp_path / "x.segments.json"
    t.to_json(p)
    data = json.loads(p.read_text())
    assert "words" not in data["segments"][0]
    assert data["segments"][0]["low_conf"] == [[0, 1, 0.3, "탐나"]]
    assert "low_conf" not in data["segments"][1]
    back = Transcript.from_json(p)
    assert back.segments[0].low_conf == [[0, 1, 0.3, "탐나"]]
    # low_conf 없는 옛 파일도 읽힌다
    data["segments"][0].pop("low_conf")
    p.write_text(json.dumps(data))
    assert Transcript.from_json(p).segments[0].low_conf == []


def test_parse_attendees_with_org():
    from rec2md.series import parse_attendees_full
    got = parse_attendees_full("# 원내·외부 모두\n홍길동\t가상연구원\n김철수\t가상도청 담당\n이영희 박민수\n홍길동\n")
    assert got == {"홍길동": "가상연구원", "김철수": "가상도청 담당", "이영희": "", "박민수": ""}


def test_attendees_from_clova_labels(tmp_path):
    from rec2md.series import attendees_from_clova
    clova = tmp_path / "c.txt"
    body = ["제목", "2026.01.01 Thu AM 10:00 ・ 5Minutes", "홍길동 김철수 가상도청 기획팀장 가상도청 주무관님 (성함확인필요)", "",
            "홍길동 00:00", "시작합니다", "", "김철수 가상도청 기획팀장 00:10", "네", "",
            "가상도청 주무관님 (성함확인필요) 00:20", "예", "", "Attendees 3 00:30", "음", "", "홍길동 00:40", "끝"]
    clova.write_text("\n".join(body) + "\n", encoding="utf-8")
    assert attendees_from_clova(clova) == {"홍길동": "", "김철수": "가상도청 기획팀장"}


def test_parse_glossary():
    terms, variants = parse_glossary("# 주석\n탐라데이터센터\n한나산 => 한라산\n\n탐라데이터센터\n")
    assert terms == ["탐라데이터센터", "한라산"]
    assert variants == {"한나산": "한라산"}


def test_series_cli_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REC2MD_SERIES_DIR", str(tmp_path))
    clova = tmp_path / "c.txt"
    clova.write_text("제목\n2026.01.01 Thu AM 10:00 ・ 1Minutes\n홍길동 김철수\n\n홍길동 00:00\n안녕\n\n김철수 00:05\n네\n",
                     encoding="utf-8")
    series_main(["init", "가상위", "--from-clova", str(clova)])
    series_main(["add", "가상위", "--term", "탐라데이터센터", "--fix", "한나산 => 한라산", "--attendee", "이몽룡=가상도청"])
    ctx = load_series("가상위")
    assert ctx.attendees == ["홍길동", "김철수", "이몽룡"]
    assert ctx.terms == ["탐라데이터센터", "한라산"]
    assert build_prompt(ctx) == "홍길동, 김철수, 이몽룡, 탐라데이터센터, 한라산"


def test_build_prompt_respects_budget():
    ctx = Context(terms=[f"용어{i:03d}" for i in range(200)])
    prompt = build_prompt(ctx, max_tokens=50)
    assert 0 < len(prompt.split(", ")) < 200
    assert len(prompt) <= 80


def test_extract_terms_skips_function_words():
    text = "탐라데이터센터 구축. 탐라데이터센터는 중요함. 그러니까 그러니까 되는데 되는데 TDX TDX"
    terms = extract_terms(text)
    assert "탐라데이터센터" in terms and "TDX" in terms
    assert "그러니까" not in terms
    assert "되는데" not in terms


def test_prompt_echo_filtered():
    prompt = "홍길동, 김철수, 탐라데이터센터"
    segs = [Segment(0, 1, "홍길동, 김철수, 탐라데이터센터"), Segment(1, 2, "홍길동"),
            Segment(2, 3, "탐라데이터센터 예산은 얼마입니까")]
    kept = [s.text for s in drop_known_hallucinations(segs, prompt)]
    assert kept == ["홍길동", "탐라데이터센터 예산은 얼마입니까"]


def test_term_ending_in_josa_is_not_a_candidate():
    # "전문가"의 "가"를 조사로 떼어 "전문"과 비교하면 맞게 적힌 용어가 후보로 오른다
    ctx = Context(terms=["전문가"])
    t = Transcript("x", 3, "local", "ko", [Segment(0, 2, "전문가 발표입니다", low_conf=[[0, 1, 0.1, "전문가"]])])
    assert all(c["suggest"] == [] for c in find_candidates(t, ctx))


def test_jamo_distance():
    assert jamo("한") == "ㅎㅏㄴ"
    assert edit_distance(jamo("홍길똥"), jamo("홍길동"), 2) == 1


def test_find_candidates():
    ctx = Context(terms=["탐라데이터센터", "한라산", "도청", "AX"], variants={"한나산": "한라산"}, attendees=["홍길동"])
    t = Transcript("x", 10, "local", "ko", [
        Segment(1, 5, "홍길동 위원님 말씀처럼 탐나데이터센터는 중요합니다", low_conf=[[2, 3, 0.31, "탐나데이터센터는"], [4, 5, 0.4, "중요합니다"]]),
        Segment(5, 6, "도정 방향과 도청", low_conf=[[5, 5.5, 0.1, "도정"]]),
        Segment(6, 9, "한나산 쪽을 보면"),
        Segment(10, 12, "홍길동 위원님 감사합니다"),
    ])
    cands = find_candidates(t, ctx)
    got = {(c["text"], tuple(c["suggest"])) for c in cands}
    assert ("탐나데이터센터", ("탐라데이터센터",)) in got
    assert ("한나산", ("한라산",)) in got
    assert not any(c["text"] == "홍길동" for c in cands)  # 맞게 적힌 건 후보가 아니다
    tam = next(c for c in cands if c["text"] == "탐나데이터센터")
    assert len(tam["reasons"]) == 2 and tam["fix"] is None  # 자모거리 + 낮은 확률이 한 후보로 합쳐진다
    assert not any(c["text"] == "중요합니다" for c in cands)  # 확률만 조금 낮은 구간은 후보가 아니다
    dojeong = next(c for c in cands if c["text"] == "도정")  # 매우 낮은 확률이면 올리되
    assert dojeong["suggest"] == []  # 2음절 용어(도청)는 근사 제안하지 않는다
