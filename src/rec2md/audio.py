"""ffmpeg로 입력(오디오·영상)을 16kHz mono wav로 정규화."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

SAMPLE_RATE = 16000


def _require(tool: str) -> str:
    path = shutil.which(tool) or _link_target(tool)
    if not path:
        raise SystemExit(f"{tool}가 PATH에 없음 (macOS: brew install ffmpeg / Windows: winget install Gyan.FFmpeg)")
    return path


def _link_target(tool: str) -> str | None:
    """PATH의 심링크를 직접 따라간다.

    winget은 `WinGet\\Links\\ffprobe.exe`를 심링크로 두는데, ssh로 들어온 세션의 프로세스는 이를 따라가지
    못해(WinError 448, 신뢰할 수 없는 탑재 지점) shutil.which가 None을 준다(namun-ji 실측 2026-11-04).
    """
    import os
    names = [tool] + ([tool + ".exe"] if os.name == "nt" and not tool.lower().endswith(".exe") else [])
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for n in names:
            p = os.path.join(d, n)
            if os.path.islink(p):
                t = os.readlink(p).removeprefix("\\\\?\\")
                t = t if os.path.isabs(t) else os.path.join(d, t)
                if os.path.isfile(t):
                    return t
    return None


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [_require("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


# 브라우저 <audio>가 재생하는 코덱(Chrome·Safari·Firefox 공통에 가까운 것만). 녹음기 중에는 확장자는
# .MP3인데 실제로는 MPEG-1 Layer II(mp2)로 쓰는 기기가 있어, 확장자만 보고는 재생 가능 여부를 알 수 없다.
BROWSER_CODECS = {"mp3", "aac", "opus", "vorbis", "flac", "pcm_s16le", "pcm_u8", "pcm_f32le"}


def probe_codec(path: Path) -> str | None:
    """첫 번째 오디오 스트림의 코덱 이름(ffprobe codec_name). 못 읽으면 None."""
    try:
        out = subprocess.run(
            [_require("ffprobe"), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        streams = json.loads(out).get("streams") or []
        return streams[0].get("codec_name") if streams else None
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None


def to_mp3(src: Path, dst: Path) -> Path:
    """브라우저 재생용 mp3 사본(첫 오디오 스트림, 96kbps)."""
    tmp = dst.with_name(dst.name + ".part")
    subprocess.run(
        [_require("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "96k", "-f", "mp3", str(tmp)],
        check=True,
    )
    tmp.replace(dst)
    return dst


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
