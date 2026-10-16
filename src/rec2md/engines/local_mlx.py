"""Apple Silicon 로컬 전사: mlx-whisper."""

from __future__ import annotations

from pathlib import Path

from ..segments import Segment, Word
from .base import Engine

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


class LocalMLX(Engine):
    name = "local"

    def __init__(self, model: str | None = None):
        self.model = model or DEFAULT_MODEL

    def transcribe(self, wav: Path, language: str) -> list[Segment]:
        try:
            import mlx_whisper
        except ImportError as e:
            raise SystemExit("local 엔진은 Apple Silicon Mac + mlx-whisper가 필요함. Windows는 --remote studio 사용") from e

        result = mlx_whisper.transcribe(
            str(wav),
            path_or_hf_repo=self.model,
            language=language,
            word_timestamps=True,
            # 이전 문맥을 넘기면 한 번 생긴 환각이 반복 루프로 번진다
            condition_on_previous_text=False,
            # 긴 무음 구간에서 지어낸 문장을 건너뛴다 (word_timestamps 필요)
            hallucination_silence_threshold=2.0,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            verbose=None,
        )
        segs = []
        for s in result["segments"]:
            words = [Word(w["start"], w["end"], w["word"], prob=w.get("probability", 1.0)) for w in s.get("words", [])]
            segs.append(Segment(start=s["start"], end=s["end"], text=s["text"].strip(), words=words))
        return segs
