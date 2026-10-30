"""합성 테스트 픽스처 생성기.

macOS 환경에서는 `say` 명령어(Yuna, Eddy 음성)와 ffmpeg를 사용하여 16kHz mono PCM wav를 생성하고,
비-macOS 환경에서는 표준 라이브러리 `wave`를 사용하여 합성 톤 wav를 생성합니다.
또한 텍스트 및 JSON 픽스처(clova txt, transcript json, minutes json)를 함께 생성합니다.

모든 픽스처는 가상/합성 데이터만 사용하며 실제 개인정보나 음성은 포함하지 않습니다.
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def generate_wav(out_path: Path) -> Path:
    """합성 16kHz mono PCM wav 파일 생성."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    has_say = shutil.which("say") is not None
    has_ffmpeg = shutil.which("ffmpeg") is not None

    if has_say and has_ffmpeg:
        # macOS say + ffmpeg로 두 화자의 대화 생성
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            p1 = tmp / "spk1.aiff"
            p2 = tmp / "spk2.aiff"
            subprocess.run(["say", "-v", "Yuna", "안녕하세요. 오늘 회의를 시작하겠습니다.", "-o", str(p1)], check=True)
            subprocess.run(["say", "-v", "Eddy", "네, 첫 번째 안건을 검토하겠습니다.", "-o", str(p2)], check=True)

            concat_list = tmp / "concat.txt"
            concat_list.write_text(f"file '{p1}'\nfile '{p2}'\n", encoding="utf-8")

            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_path)
                ],
                check=True,
            )
            return out_path

    # Fallback: 표준 라이브러리 wave로 16kHz mono PCM 톤 생성 (화자 2명 대화 구간 모사)
    sample_rate = 16000
    # 0.0~1.5s: 화자 1 (440Hz 톤), 1.5~2.0s: 무음, 2.0~3.5s: 화자 2 (660Hz 톤)
    total_samples = int(sample_rate * 3.5)
    with wave.open(str(out_path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        frames = bytearray()
        for i in range(total_samples):
            t = i / sample_rate
            if 0.0 <= t < 1.5:
                val = int(10000 * math.sin(2 * math.pi * 440 * t))
            elif 2.0 <= t < 3.5:
                val = int(10000 * math.sin(2 * math.pi * 660 * t))
            else:
                val = 0
            frames.extend(struct.pack("<h", val))
        f.writeframes(frames)
    return out_path


def generate_clova_txt(out_path: Path) -> Path:
    """클로바노트 txt 내보내기 형식 샘플 파일 생성."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "정례 주간 회의\n"
        "2026.10.30 Fri AM 10:00 ・ 3Minutes 30seconds\n"
        "참석자: 홍길동, 김철수, 이영희\n"
        "\n"
        "홍길동 00:00\n"
        "오늘 주간 회의를 시작하겠습니다. 먼저 지난주 진행 상황부터 확인하겠습니다.\n"
        "\n"
        "김철수 00:45\n"
        "네, 시스템 성능 최적화 작업이 예정대로 완료되었습니다.\n"
        "세부 지표는 공유드린 문서를 참고해 주시기 바랍니다.\n"
        "\n"
        "이영희 01:30\n"
        "사용자 인터페이스 개편안도 시안 작업이 끝났습니다.\n"
        "\n"
        "홍길동 02:15\n"
        "좋습니다. 다음 주 배포 일정에 맞춰 마무리해 주시기 바랍니다.\n"
    )
    out_path.write_text(content, encoding="utf-8-sig")
    return out_path


def generate_minutes_json(out_path: Path) -> Path:
    """docs/minutes-schema.md에 부합하는 합성 회의록 JSON 생성."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "title": "합성 테스트 회의",
        "date": "2026-10-30",
        "time": "10:00-11:00",
        "place": "가상 회의실",
        "agenda": ["테스트 자동화 계획 수립", "일정 검토"],
        "attendees": [
            {"name": "홍길동", "org": "가나연구원", "role": "연구위원"},
            {"name": "김철수", "org": "가나연구원", "role": "선임연구원"},
        ],
        "author": "김철수",
        "owner": "홍길동",
        "summary": ["테스트 자동화 및 CI 파이프라인 구축 안건 확정"],
        "sections": [
            {
                "heading": "테스트 프레임워크 선정",
                "items": [
                    {
                        "text": "pytest 도입",
                        "children": [{"text": "단위 테스트 및 합성 픽스처 적용"}],
                        "ts": 12.5,
                    }
                ],
            }
        ],
        "decisions": ["pytest를 공식 테스트 러너로 채택"],
        "actions": [
            {"task": "CI 워크플로 작성", "owner": "김철수", "due": "2026-10-31"}
        ],
        "next_meeting": "2026-11-06 10:00",
        "attachments": [],
        "source": {
            "recording": "sample.wav",
            "transcript": "sample.segments.json",
            "range": [0, 3600],
        },
        "version": 1,
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_path


def generate_transcript_json(out_path: Path) -> Path:
    """rec2md Transcript 공통 스키마 샘플 생성."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "source": "sample_dialogue.wav",
        "duration": 180.0,
        "engine": "local",
        "language": "ko",
        "segments": [
            {
                "start": 0.0,
                "end": 3.5,
                "text": "안녕하세요. 오늘 회의를 시작하겠습니다.",
                "speaker": "화자1",
            },
            {
                "start": 4.0,
                "end": 8.0,
                "text": "네, 첫 번째 안건을 검토하겠습니다.",
                "speaker": "화자2",
            },
            {
                "start": 8.5,
                "end": 12.0,
                "text": "세부 일정에 대해 말씀해 주십시오.",
                "speaker": "화자1",
            },
        ],
        "meta": {"diarizer": "pyannote/speaker-diarization-community-1", "title": "주간 회의"},
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_path


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = generate_wav(FIXTURES_DIR / "sample_dialogue.wav")
    print(f"Generated: {wav_path} ({wav_path.stat().st_size} bytes)")

    clova_path = generate_clova_txt(FIXTURES_DIR / "sample_clova.txt")
    print(f"Generated: {clova_path}")

    min_path = generate_minutes_json(FIXTURES_DIR / "sample_minutes.json")
    print(f"Generated: {min_path}")

    tx_path = generate_transcript_json(FIXTURES_DIR / "sample_transcript.json")
    print(f"Generated: {tx_path}")


if __name__ == "__main__":
    main()
