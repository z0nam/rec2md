"""Apple Silicon 로컬 전사: mlx-whisper."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ..audio import SAMPLE_RATE, load_wav
from ..segments import Segment, Word
from .base import Engine, Language, Region

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
PAD = 0.2  # 구간 앞뒤로 붙이는 여유(초). 첫 음절이 잘리지 않게
# mlx 버퍼 캐시 상한(바이트). mlx 기본값은 메모리 한도와 같아서 다 쓴 버퍼가 캐시로 쌓인다
# (1시간 전사에서 활성 2.4GB에 캐시 7.5GB). 1GB로 묶어도 전사 시간·결과는 반복 실행의 편차 안이었다.
# 근거는 docs/PLAN.md "긴 녹음 메모리 실측"
CACHE_LIMIT = 1 * 2**30


@contextmanager
def _cache_limit(limit: int):
    """전사하는 동안만 mlx 캐시 상한을 걸고, 끝나면 캐시를 비우고 이전 상한으로 돌린다."""
    import mlx.core as mx

    prev = mx.set_cache_limit(limit)
    try:
        yield
    finally:
        mx.set_cache_limit(prev)
        mx.clear_cache()


def _require_mlx() -> None:
    try:
        import mlx_whisper  # noqa: F401
    except ImportError as e:
        raise SystemExit("local 엔진은 Apple Silicon Mac + mlx-whisper가 필요함. Windows는 --remote studio 사용") from e


class LocalMLX(Engine):
    name = "local"

    def __init__(self, model: str | None = None, prompt: str | None = None):
        self.model = model or DEFAULT_MODEL
        # 용어집 프롬프트. condition_on_previous_text=False라 30초 창마다 초기화되므로
        # 청크(25초 이하) 단위로 넘기면 모든 청크에 적용된다. 청킹 없이 통째로 넣으면 첫 창에만 걸린다
        self.prompt = prompt or None

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
            initial_prompt=self.prompt,
            verbose=None,
        )
        segs = []
        for s in result["segments"]:
            words = [Word(w["start"] + offset, w["end"] + offset, w["word"], prob=w.get("probability", 1.0))
                     for w in s.get("words", [])]
            segs.append(Segment(start=s["start"] + offset, end=s["end"] + offset, text=s["text"].strip(), words=words))
        return segs

    def transcribe(self, wav: Path, language: Language, regions: list[Region] | None = None) -> list[Segment]:
        _require_mlx()
        with _cache_limit(CACHE_LIMIT):
            return self._transcribe(wav, language, regions)

    def language_probs(self, clip) -> dict[str, float]:
        """창(30초 이하)의 언어별 확률. 인코더 한 번만 돌린다."""
        _require_mlx()
        import mlx.core as mx
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        from mlx_whisper.transcribe import ModelHolder

        with _cache_limit(CACHE_LIMIT):
            # transcribe()와 같은 방식으로 첫 30초 창의 언어 확률을 낸다(모델은 ModelHolder가 한 번만 올린다)
            model = ModelHolder.get_model(self.model, mx.float16)
            mel = log_mel_spectrogram(clip, n_mels=model.dims.n_mels, padding=N_SAMPLES)
            _, probs = model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
        return {k: float(v) for k, v in probs.items()}

    def detect_language(self, clip) -> tuple[dict[str, float], str]:
        import mlx_whisper

        probs = self.language_probs(clip)
        with _cache_limit(CACHE_LIMIT):
            text = mlx_whisper.transcribe(clip, path_or_hf_repo=self.model, language=None,
                                          condition_on_previous_text=False, verbose=None)["text"]
        return probs, text

    def _transcribe(self, wav: Path, language: Language, regions: list[Region] | None) -> list[Segment]:
        if not regions:
            if callable(language):  # 구간이 없으면 파일 전체를 한 덩어리로 본다
                audio = load_wav(wav)
                return self._decode(audio, language(audio, 0.0, len(audio) / SAMPLE_RATE), 0.0)
            return self._decode(str(wav), language, 0.0)

        # 말소리 구간만 잘라 따로 전사한다. 통째로 넣으면 다화자·원거리 녹음에서
        # 30초 창 단위로 문장을 건너뛰는 일이 잦다(실측: 클로바 대비 분량 59〜69% → 청킹 후 개선)
        audio = load_wav(wav)
        segs: list[Segment] = []
        prev_b = 0.0
        for s, e in regions:
            # 붙어서 쪼개진 청크끼리 패딩이 겹치면 경계 단어가 두 번 전사되므로 앞 청크 끝 이후로 민다
            a, b = max(prev_b, s - PAD), e + PAD
            prev_b = b
            clip = audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)]
            if len(clip) < SAMPLE_RATE // 2:
                continue
            segs += self._decode(clip, language(clip, a, b) if callable(language) else language, a)
        return segs
