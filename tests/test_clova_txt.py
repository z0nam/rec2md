"""clova_txt 모듈 단위 테스트: 클로바노트 txt 파싱 및 Transcript 변환."""

from pathlib import Path

from rec2md.clova_txt import _sec, parse

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_sec_parser():
    assert _sec("00:00") == 0.0
    assert _sec("01:23") == 83.0
    assert _sec("01:02:03") == 3723.0


def test_parse_sample_fixture():
    fixture_path = FIXTURES_DIR / "sample_clova.txt"
    t = parse(fixture_path)

    assert t.source == "sample_clova.txt"
    assert t.engine == "clova-note"
    assert t.language == "ko"
    assert t.meta.get("title") == "정례 주간 회의"
    assert t.duration == 210.0  # 3분 30초 = 210초

    assert len(t.segments) == 4

    # 첫 번째 세그먼트
    s0 = t.segments[0]
    assert s0.speaker == "홍길동"
    assert s0.start == 0.0
    assert s0.end == 45.0  # 다음 발화의 시작 시각
    assert "주간 회의를 시작하겠습니다" in s0.text

    # 두 번째 세그먼트 (여러 줄 텍스트 병합 확인)
    s1 = t.segments[1]
    assert s1.speaker == "김철수"
    assert s1.start == 45.0
    assert s1.end == 90.0
    assert "시스템 성능 최적화" in s1.text
    assert "세부 지표는" in s1.text

    # 마지막 세그먼트 end 확인 (전체 duration과 동일)
    s_last = t.segments[-1]
    assert s_last.speaker == "홍길동"
    assert s_last.start == 135.0  # 02:15
    assert s_last.end == 210.0


def test_parse_korean_duration_formats(tmp_path: Path):
    txt_content = (
        "예산 심의 회의\n"
        "2026.10.30 ・ 1시간 15분 30초\n"
        "참석자: 김위원, 이위원\n"
        "\n"
        "김위원 00:10\n"
        "예산안을 살펴보겠습니다.\n"
        "\n"
        "이위원 01:00\n"
        "동의합니다.\n"
    )
    p = tmp_path / "clova_ko.txt"
    p.write_text(txt_content, encoding="utf-8-sig")

    t = parse(p)
    assert t.duration == 3600 + 15 * 60 + 30  # 4530초
    assert len(t.segments) == 2
    assert t.segments[0].start == 10.0
    assert t.segments[0].end == 60.0
    assert t.segments[1].end == 4530.0
