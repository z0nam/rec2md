"""minutes 모듈 단위 테스트: 회의록 공통 스키마 및 직렬화/역직렬화."""

from pathlib import Path

from rec2md.minutes import SCHEMA_VERSION, Action, Attendee, Item, Minutes, Section

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_minutes_from_sample_fixture():
    fixture_path = FIXTURES_DIR / "sample_minutes.json"
    m = Minutes.from_json(fixture_path)

    assert m.version == SCHEMA_VERSION
    assert m.title == "합성 테스트 회의"
    assert m.date == "2026-10-30"
    assert m.time == "10:00-11:00"
    assert m.place == "가상 회의실"
    assert len(m.agenda) == 2
    assert len(m.attendees) == 2

    # Attendee 파싱 확인
    assert m.attendees[0].name == "홍길동"
    assert m.attendees[0].org == "가나연구원"
    assert m.attendees[0].role == "연구위원"

    # 재귀 Item 구조 확인
    assert len(m.sections) == 1
    sec = m.sections[0]
    assert sec.heading == "테스트 프레임워크 선정"
    assert len(sec.items) == 1
    item = sec.items[0]
    assert item.text == "pytest 도입"
    assert item.ts == 12.5
    assert len(item.children) == 1
    assert item.children[0].text == "단위 테스트 및 합성 픽스처 적용"

    # Decisions / Actions 확인
    assert m.decisions == ["pytest를 공식 테스트 러너로 채택"]
    assert len(m.actions) == 1
    assert m.actions[0].task == "CI 워크플로 작성"
    assert m.actions[0].due == "2026-10-31"


def test_minutes_json_roundtrip(tmp_path: Path):
    m1 = Minutes(
        title="라운드트립 검증 회의",
        date="2026-11-01",
        time="14:00-15:30",
        place="본관 회의실",
        agenda=["주요 안건 1", "주요 안건 2"],
        attendees=[Attendee(name="이순신", org="조선수군", role="통제사")],
        author="이순신",
        summary=["전황 점검 완료"],
        sections=[
            Section(
                heading="군사 대비 태세",
                items=[
                    Item(
                        text="거북선 건조 진행률",
                        ts=120.0,
                        children=[
                            Item(
                                text="철갑 보강 완료",
                                children=[Item(text="세부 3단 항목 검증")],
                            )
                        ],
                    )
                ],
            )
        ],
        decisions=["출정 준비 완료"],
        actions=[Action(task="무기 점검", owner="이순신", due="11/02")],
    )

    out_file = tmp_path / "test.minutes.json"
    m1.to_json(out_file)

    m2 = Minutes.from_json(out_file)
    assert m2.title == m1.title
    assert m2.date == m1.date
    assert len(m2.sections) == 1
    # 3단 중첩 아이템 확인
    assert m2.sections[0].items[0].children[0].children[0].text == "세부 3단 항목 검증"
    assert m2.actions[0].task == "무기 점검"
