"""segments 모듈 단위 테스트: Transcript / Segment 스키마 및 JSON 저장/로드."""

from pathlib import Path

from rec2md.segments import Segment, Transcript, Word

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_transcript_from_fixture():
    fixture_path = FIXTURES_DIR / "sample_transcript.json"
    t = Transcript.from_json(fixture_path)

    assert t.source == "sample_dialogue.wav"
    assert t.duration == 180.0
    assert t.engine == "local"
    assert len(t.segments) == 3
    assert t.segments[0].speaker == "화자1"
    assert t.segments[1].speaker == "화자2"


def test_transcript_to_json_strips_words(tmp_path: Path):
    # to_json 저장 시 words는 중간물이므로 제외되어야 함
    t = Transcript(
        source="test.wav",
        duration=10.0,
        engine="local",
        language="ko",
        segments=[
            Segment(
                0.0,
                3.0,
                "테스트 발화입니다.",
                speaker="화자1",
                words=[Word(0.0, 1.0, "테스트"), Word(1.1, 3.0, "발화입니다.")],
            )
        ],
    )
    out_file = tmp_path / "test.segments.json"
    t.to_json(out_file)

    loaded = Transcript.from_json(out_file)
    assert len(loaded.segments) == 1
    assert loaded.segments[0].text == "테스트 발화입니다."
    assert loaded.segments[0].words == []
