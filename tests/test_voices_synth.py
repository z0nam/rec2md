"""합성 화자 3명(macOS say)으로 녹취 두 개를 만들어 하나로 등록하고 다른 하나에서 맞히는지.

pyannote·HF_TOKEN·macOS say가 있어야 돌고, 없으면 건너뛴다(CI 리눅스에서는 건너뜀).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.slow
pytest.importorskip("pyannote.audio")
if not shutil.which("say") or not shutil.which("ffmpeg"):
    pytest.skip("macOS say·ffmpeg 필요", allow_module_level=True)

from dotenv import load_dotenv  # noqa: E402

from rec2md import voices  # noqa: E402
from rec2md.audio import SAMPLE_RATE, normalize  # noqa: E402
from rec2md.cli import ENV_FILE  # noqa: E402

load_dotenv(ENV_FILE)
if not os.environ.get("HF_TOKEN"):
    pytest.skip("HF_TOKEN 필요", allow_module_level=True)

VOICES = {"홍길동": "Yuna", "김철수": "Rocko (Korean (South Korea))", "이영희": "Grandma (Korean (South Korea))"}
LINES = [
    "오늘 회의에서는 다음 달 예산 집행 계획을 함께 검토하겠습니다.",
    "지난번에 말씀드린 자료는 아직 정리 중이라서 다음 주에 공유드릴게요.",
    "현장 조사 결과를 보면 방문객 수가 작년보다 조금 늘었습니다.",
    "그 부분은 담당 부서와 한 번 더 확인해 보는 게 좋겠습니다.",
    "일정이 빠듯하니까 우선순위를 정해서 진행하면 어떨까요.",
    "보고서 초안은 이번 주 금요일까지 마무리하는 것으로 하겠습니다.",
]


def _say(text: str, voice: str, out: Path) -> np.ndarray:
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    normalize(aiff, out)
    with wave.open(str(out), "rb") as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)


def make_dialogue(tmp: Path, name: str, order: list[str]) -> tuple[Path, list[tuple[float, float, str]]]:
    """화자를 번갈아 말하게 이어 붙인 wav와 정답 구간."""
    gap = np.zeros(int(0.6 * SAMPLE_RATE), dtype=np.int16)
    parts, truth, t = [], [], 0.0
    for k, who in enumerate(order):
        pcm = _say(LINES[k * 5 % len(LINES)], VOICES[who], tmp / f"{name}_{k}.wav")
        truth.append((t, t + len(pcm) / SAMPLE_RATE, who))
        parts += [pcm, gap]
        t += (len(pcm) + len(gap)) / SAMPLE_RATE
    out = tmp / f"{name}.wav"
    with wave.open(str(out), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(np.concatenate(parts).tobytes())
    return out, truth


def label_truth(turns, truth) -> dict[str, str]:
    """pyannote 라벨 → 겹침이 가장 큰 정답 화자."""
    ov: dict[tuple[str, str], float] = {}
    for s, e, spk in turns:
        for ts, te, who in truth:
            ov[spk, who] = ov.get((spk, who), 0.0) + max(0.0, min(e, te) - max(s, ts))
    return {spk: max((v, w) for (x, w), v in ov.items() if x == spk)[1] for _, _, spk in turns}


def test_enroll_then_match_synthetic(tmp_path):
    from rec2md.diarize import diarize_with_embeddings

    order = ["홍길동", "김철수", "이영희", "김철수", "홍길동", "이영희"] * 2
    recs = {}
    for name, seq in [("a", order), ("b", order[::-1])]:
        wav, truth = make_dialogue(tmp_path, name, seq)
        turns, embs, segs = diarize_with_embeddings(wav, num_speakers=3)
        assert segs and {x[2] for x in segs} <= set(embs)
        lt = label_truth(turns, truth)
        rename = {raw: f"화자{i + 1}" for i, raw in enumerate(sorted(embs))}
        side = tmp_path / f"{name}.voices.npz"
        voices.save_sidecar(side, embs, turns, rename, segs)
        recs[name] = (voices.load_sidecar(side), {rename[r]: lt[r] for r in rename}, voices.load_segments(side))

    db = voices.VoiceDB(":memory:")
    side_a, truth_a, segs_a = recs["a"]
    voices.enroll(db, side_a, truth_a, "a.wav", min_sec=5, segments=segs_a)
    side_b, truth_b, _ = recs["b"]
    got = voices.match(db.vectors(), side_b)
    assert {lb: d["name"] for lb, d in got.items()} == truth_b, got
