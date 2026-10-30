"""audio 모듈 단위 테스트: probe_duration, normalize 등 음원 전처리 (ffmpeg 의존)."""

import shutil
from pathlib import Path

import pytest
from rec2md.audio import normalize, probe_duration

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SAMPLE_WAV = FIXTURES_DIR / "sample_dialogue.wav"

has_ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg/ffprobe가 PATH에 없음")
def test_probe_duration():
    duration = probe_duration(SAMPLE_WAV)
    assert 2.0 <= duration <= 10.0


@pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg/ffprobe가 PATH에 없음")
def test_normalize_audio(tmp_path: Path):
    dst = tmp_path / "normalized.wav"
    out = normalize(SAMPLE_WAV, dst)
    assert out.exists()
    assert out.stat().st_size > 0
    duration = probe_duration(out)
    assert duration > 0
