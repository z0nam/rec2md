"""전사 전 거친 자르기: 녹음기를 끄지 않아 생긴 긴 무음을 빼고 말소리 구간만 이어 붙인다.

회의 시작·끝 추정(`bounds`)과는 다르다. 이것은 화자분리·전사 전에, 소리 크기만 보고 몇 분 이상
조용한 구간을 잘라 처리 시간과 메모리를 줄이는 단계다. 자른 뒤의 시각은 오프셋 표로 원본 기준으로 되돌린다.

판정 (1초 단위 RMS dBFS):
- 임계값: 파일의 잡음 바닥(하위 20%) + 15dB를 [-45, -30]dB로 묶는다. 녹음기의 "무음"은 0이 아니라
  잡음이다: 조용한 방 -50〜-57dB(임계 -37dB), 시끄러운 방은 바닥이 -40dB라 상한 -30dB에 걸린다.
  상한이 -35dB이면 시끄러운 방의 잡음이 말소리로 잡혀 5시간 38분 녹음이 4시간 22분 남았다(-30dB면 3시간 40분).
  -30dB로 새로 잘리는 구간 5곳을 Whisper로 들어 보니 4곳은 무음 환각뿐, 1곳은 먼 말소리 조각일 수 있었다.
- 1분 창에서 10% 이상이 임계값을 넘으면 말소리 창. 말소리 창 앞뒤로 PAD만큼 남긴다.
- 그 사이 비는 곳이 MIN_GAP(기본 5분, `--trim-min-gap`) 이상일 때만 잘라낸다. 애매하면 남긴다 — 남기면 시간만 들지만 자르면 내용을 잃는다.
"""

from __future__ import annotations

import subprocess
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

import numpy as np

from .audio import SAMPLE_RATE, _require

MIN_GAP = 300.0      # 이보다 긴 무음만 자른다(초)
WINDOW = 60          # 말소리 판정 창(초)
ACTIVE_FRAC = 0.10   # 창 안에서 임계값을 넘는 초의 비율
PAD = 15.0           # 말소리 창 앞뒤로 남길 여유(초)
FLOOR_PCT, MARGIN, T_MIN, T_MAX = 20, 15.0, -45.0, -30.0


def loudness_file(path, rate: int = 8000) -> np.ndarray:
    """ffmpeg로 흘려 읽으며 1초마다 RMS dBFS. 6시간 파일도 메모리 수십 MB, 십수 초."""
    p = subprocess.Popen([_require("ffmpeg"), "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
                          "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"], stdout=subprocess.PIPE)
    out = []
    while chunk := p.stdout.read(rate * 2):
        x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        out.append(20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-9))
    if p.wait() != 0:
        raise RuntimeError(f"ffmpeg로 소리 크기를 읽지 못함: {path}")
    return np.array(out, dtype=np.float32)


def loudness(wav: np.ndarray, rate: int = SAMPLE_RATE) -> np.ndarray:
    """이미 읽은 float32 배열에서 1초마다 RMS dBFS."""
    n = len(wav) // rate
    if n == 0:
        return np.array([20 * np.log10(np.sqrt(np.mean(wav * wav)) + 1e-9)], dtype=np.float32)
    x = wav[: n * rate].reshape(n, rate)
    db = 20 * np.log10(np.sqrt(np.mean(x * x, axis=1)) + 1e-9)
    if len(wav) > n * rate:
        tail = wav[n * rate:]
        db = np.append(db, 20 * np.log10(np.sqrt(np.mean(tail * tail)) + 1e-9))
    return db.astype(np.float32)


def threshold(db: np.ndarray) -> float:
    return float(np.clip(np.percentile(db, FLOOR_PCT) + MARGIN, T_MIN, T_MAX))


REVIEW_FRAC = ACTIVE_FRAC / 2  # 잘라낸 구간 중 이만큼 소리가 있던 1분 창이 있으면 "다시 들어 볼 구간"


@dataclass
class Plan:
    keep: list[tuple[float, float]]   # 남길 구간(원본 초)
    cut: list[tuple[float, float]]    # 잘라낼 구간(원본 초)
    duration: float
    threshold: float
    review: list[tuple[float, float]] = None  # 잘라냈지만 약한 소리가 있던 1분 창(원본 초)
    min_gap: float = MIN_GAP

    @property
    def kept_sec(self) -> float:
        return sum(e - s for s, e in self.keep)

    def trims(self) -> bool:
        return bool(self.cut)


def plan(db: np.ndarray, min_gap: float = MIN_GAP) -> Plan:
    """1초 단위 dBFS → 남길 구간과 잘라낼 구간."""
    dur = float(len(db))
    t = threshold(db)
    active = db > t
    n_win = max(1, int(np.ceil(len(db) / WINDOW)))
    speech = []
    for i in range(n_win):
        w = active[i * WINDOW:(i + 1) * WINDOW]
        if len(w) and w.mean() >= ACTIVE_FRAC:
            speech.append((max(0.0, i * WINDOW - PAD), min(dur, (i + 1) * WINDOW + PAD)))
    keep: list[tuple[float, float]] = []
    for s, e in speech:  # 창을 이어 붙이되, 틈이 min_gap보다 짧으면 메운다
        if keep and s - keep[-1][1] < min_gap:
            keep[-1] = (keep[-1][0], max(keep[-1][1], e))
        else:
            keep.append((s, e))
    if not keep:  # 말소리를 못 찾으면 자르지 않는다(판정을 믿지 않는 편이 안전)
        return Plan([(0.0, dur)], [], dur, t, [], min_gap)
    # 처음·끝의 짧은 무음은 남긴다 — 앞뒤도 min_gap 이상일 때만 자른다
    if keep[0][0] < min_gap:
        keep[0] = (0.0, keep[0][1])
    if dur - keep[-1][1] < min_gap:
        keep[-1] = (keep[-1][0], dur)
    cut, prev = [], 0.0
    for s, e in keep:
        if s > prev:
            cut.append((prev, s))
        prev = e
    if prev < dur:
        cut.append((prev, dur))
    return Plan(keep, cut, dur, t, _review(active, cut), min_gap)


def _review(active: np.ndarray, cut: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """잘라낸 구간 안에서 말소리 기준의 절반 이상 소리가 있던 1분 창. 먼 말소리일 수 있어 사람이 들어 볼 후보."""
    out: list[tuple[float, float]] = []
    for s, e in cut:
        for w0 in range(int(s), int(e), WINDOW):
            w1 = min(int(e), w0 + WINDOW)
            if w1 - w0 >= WINDOW // 2 and active[w0:w1].mean() >= REVIEW_FRAC:
                if out and out[-1][1] >= w0:
                    out[-1] = (out[-1][0], float(w1))
                else:
                    out.append((float(w0), float(w1)))
    return out


class OffsetMap:
    """이어 붙인 오디오의 시각 ↔ 원본 시각."""

    def __init__(self, keep: list[tuple[float, float]]):
        self.keep = keep
        self.starts, acc = [], 0.0  # 이어 붙인 오디오에서 각 구간이 시작하는 시각
        for s, e in keep:
            self.starts.append(acc)
            acc += e - s
        self.length = acc

    def to_original(self, t: float, end: bool = False) -> float:
        """`end`면 구간의 끝 시각: 이어 붙인 경계에 정확히 걸리면 앞 구간의 끝으로 본다
        (뒤 구간의 시작으로 보면 잘라낸 공백 전체가 그 구간에 들어간다)."""
        i = max(0, (bisect_left if end else bisect_right)(self.starts, t) - 1)
        s, e = self.keep[i]
        return min(s + (t - self.starts[i]), e)

    def table(self) -> list[list[float]]:
        """[[이어 붙인 시각, 원본 시작, 원본 끝], …] — meta에 남겨 사람이 되짚을 수 있게."""
        return [[round(a, 2), round(s, 2), round(e, 2)] for a, (s, e) in zip(self.starts, self.keep)]


def cut_audio(wav: np.ndarray, keep: list[tuple[float, float]], rate: int = SAMPLE_RATE) -> np.ndarray:
    return np.concatenate([wav[int(s * rate):int(e * rate)] for s, e in keep])


def remap_segments(segs: list, m: OffsetMap) -> None:
    for s in segs:
        s.start, s.end = m.to_original(s.start), m.to_original(s.end, end=True)
        for w in getattr(s, "words", None) or []:
            w.start, w.end = m.to_original(w.start), m.to_original(w.end, end=True)
        if getattr(s, "low_conf", None):
            for sp in s.low_conf:
                sp[0], sp[1] = round(m.to_original(sp[0]), 2), round(m.to_original(sp[1], end=True), 2)


def remap_spans(spans: list[tuple], m: OffsetMap) -> list[tuple]:
    """(start, end, …) 튜플 목록(화자분리 turn, 구간 임베딩)의 앞 두 값을 원본 시각으로."""
    return [(m.to_original(a), m.to_original(b, end=True), *rest) for a, b, *rest in spans]


def meta(p: Plan, m: OffsetMap) -> dict:
    return {"threshold_db": round(p.threshold, 1), "min_gap": p.min_gap, "kept_sec": round(p.kept_sec, 1),
            "cut": [[round(s, 1), round(e, 1)] for s, e in p.cut],
            "review": [[round(s, 1), round(e, 1)] for s, e in p.review or []], "offsets": m.table()}
