"""dry-run 말소리 추정 보정: 소리 크기로 남긴 구간에서 표본 창만 골라 VAD를 돌려 "소리는 있는데 말소리가
아닌" 비율을 잰다(`rec2md batch --dry-run --speech-check`).

소리 크기 추정(`trim`)은 지속 소음(차 안 주변음, 환풍기)을 말소리로 센다. 5시간 25분 녹음에서 trim이 2:03:45를
남겼는데 전사된 발화 구간은 약 4분이었다. 그래서 남긴 구간 전체에 고르게 30초 창 N개를 놓고 pyannote
segmentation(화자분리 파이프라인과 같은 모델)으로 말소리 비율을 잰 뒤, 남긴 길이에 곱해 말소리 길이를 다시 추정한다.

- CPU에서 돈다(GPU 전사와 겹치지 않게). 창 12개면 파일 하나에 약 30초(소리 크기 추정 포함).
- 실측(남긴 길이 → 창 12개 / 창 24개 추정 / 전사된 발화 구간): 소음 녹음 124분 → 1.6 / 5.7 / 4.1분,
  원거리 회의 249분 → 62 / 73 / 76분, 통화+긴 공백 63분 → 6.3 / 8.9 / 10.4분, 근접 회의 82분 → 75 / 73 / 74분.
  12개는 조금 낮게 잡지만 소음 위주와 회의를 가르기에는 충분하다. 자세한 표는 docs/PLAN.md.
- 비율이 NOISY 미만이면 "소음 위주?"로 표시한다. 건너뛰지는 않는다 — 사람이 보고 거른다.
- pyannote나 HF_TOKEN이 없으면(Windows의 `batch --remote` 쪽 등) 보정하지 않고 None을 돌려준다.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np

from . import trim
from .audio import SAMPLE_RATE, _require, probe_duration

WINDOWS = 12     # 표본 창 수
WIN = 30.0       # 창 길이(초)
NOISY = 0.15     # 말소리 비율이 이보다 낮으면 소음 위주로 의심

_vad = None


def windows(keep: list[tuple[float, float]], n: int = WINDOWS, win: float = WIN) -> list[tuple[float, float]]:
    """남긴 구간들을 이어 붙인 길이 위에 n개 창을 고르게 놓는다 → [(원본 시작 초, 길이)].

    창은 한 구간 안에 들어가게 민다. 구간이 창보다 짧으면 그 구간 전체를 쓴다."""
    total = sum(b - a for a, b in keep)
    if total <= 0 or n <= 0:
        return []
    out = []
    for k in range(n):
        t = (k + 0.5) * total / n
        for a, b in keep:
            if t < b - a:
                length = min(win, b - a)
                start = min(max(a, a + t - length / 2), b - length)
                out.append((start, length))
                break
            t -= b - a
    return out


def _clip(path: Path, start: float, dur: float) -> np.ndarray:
    raw = subprocess.run([_require("ffmpeg"), "-nostdin", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
                          "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
                         capture_output=True)
    # 깨진 프레임 경고만 있고 소리는 나오는 MP3가 있어, 실패는 종료 코드와 빈 출력이 함께일 때만 본다
    if raw.returncode != 0 and not raw.stdout:
        raise RuntimeError(f"ffmpeg로 표본을 읽지 못함 ({start:.0f}초): {raw.stderr.decode(errors='replace').strip()}")
    return np.frombuffer(raw.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _load_vad():
    """pyannote VAD(CPU). 쓸 수 없으면 None."""
    global _vad
    if _vad is None:
        token = os.environ.get("HF_TOKEN")
        if not token:
            return None
        try:
            from pyannote.audio import Model
            from pyannote.audio.pipelines import VoiceActivityDetection

            from .diarize import PIPELINE
        except ImportError:
            return None
        model = Model.from_pretrained(PIPELINE, subfolder="segmentation", token=token)
        vad = VoiceActivityDetection(segmentation=model)
        vad.instantiate({"min_duration_on": 0.0, "min_duration_off": 0.0})
        _vad = vad
    return _vad


def available() -> bool:
    try:
        return _load_vad() is not None
    except Exception:
        return False


def speech_ratio(path: Path, min_gap: float = trim.MIN_GAP, n: int = WINDOWS,
                 trimmed: bool = True) -> tuple[float, float] | None:
    """(전사할 길이, 그 안의 말소리 비율). 전사할 길이는 `trimmed`면 trim이 남길 길이, 아니면 파일 전체.
    VAD를 쓸 수 없으면 None."""
    vad = _load_vad()
    if vad is None:
        return None
    import torch

    if trimmed:
        keep = trim.plan(trim.loudness_file(path), min_gap).keep
    else:
        keep = [(0.0, probe_duration(path))]
    speech = total = 0.0
    for start, dur in windows(keep, n):
        a = _clip(path, start, dur)
        if not a.size:
            continue
        ann = vad({"waveform": torch.from_numpy(a).unsqueeze(0), "sample_rate": SAMPLE_RATE})
        speech += ann.get_timeline().support().duration()
        total += len(a) / SAMPLE_RATE
    if not total:  # 표본을 하나도 못 들었으면 0%가 아니라 추정 실패 → 보정 없이 둔다
        raise RuntimeError("말소리 확인에 쓸 표본을 하나도 읽지 못함")
    return sum(e - s for s, e in keep), min(1.0, speech / total)
