"""Apple Silicon 로컬 전사: mlx-whisper."""

from __future__ import annotations

from pathlib import Path

from ..audio import SAMPLE_RATE, load_wav
from ..segments import Segment, Word
from .base import Engine, Region

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
PAD = 0.2  # 구간 앞뒤로 붙이는 여유(초). 첫 음절이 잘리지 않게


class LocalMLX(Engine):
    name = "local"

    def __init__(self, model: str | None = None):
        self.model = model or DEFAULT_MODEL

    def _decode(self, audio, language: str, offset: float) -> list[Segment]:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            audio,
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
            words = [Word(w["start"] + offset, w["end"] + offset, w["word"], prob=w.get("probability", 1.0))
                     for w in s.get("words", [])]
            segs.append(Segment(start=s["start"] + offset, end=s["end"] + offset, text=s["text"].strip(), words=words))
        return segs

    def transcribe(self, wav: Path, language: str, regions: list[Region] | None = None) -> list[Segment]:
        try:
            import mlx_whisper  # noqa: F401
        except ImportError as e:
            raise SystemExit("local 엔진은 Apple Silicon Mac + mlx-whisper가 필요함. Windows는 --remote studio 사용") from e

        if not regions:
            return self._decode(str(wav), language, 0.0)

        # 말소리 구간만 잘라 따로 전사한다. 통째로 넣으면 다화자·원거리 녹음에서
        # 30초 창 단위로 문장을 건너뛰는 일이 잦다(실측: 클로바 대비 분량 59〜69% → 청킹 후 개선)
        audio = load_wav(wav)
        segs: list[Segment] = []
        for s, e in regions:
            a, b = max(0.0, s - PAD), e + PAD
            clip = audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)]
            if len(clip) < SAMPLE_RATE // 2:
                continue
            segs += self._decode(clip, language, a)
        return segs
