"""ffmpeg로 입력(오디오·영상)을 16kHz mono wav로 정규화."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

SAMPLE_RATE = 16000


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise SystemExit(f"{tool}가 PATH에 없음 (macOS: brew install ffmpeg / Windows: winget install Gyan.FFmpeg)")
    return path


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [_require("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def write_wav(path: Path, data) -> Path:
    """float32 배열(-1〜1) → 16bit mono wav (load_wav의 반대)."""
    import wave

    import numpy as np

    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes((np.clip(data, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return path


def load_wav(path: Path):
    """normalize()가 만든 16bit mono wav → float32 numpy 배열."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def normalize(src: Path, dst: Path) -> Path:
    """첫 번째 오디오 스트림만 뽑아 16kHz mono PCM wav로 저장. 영상이면 화면은 버린다."""
    subprocess.run(
        [_require("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )
    return dst
