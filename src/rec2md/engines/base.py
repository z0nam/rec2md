from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..segments import Segment

Region = tuple[float, float]  # 말소리 구간 (start, end), 초
# 전사 언어: 코드 하나, 또는 청크마다 고르는 함수 (청크 오디오, 시작 초, 끝 초) → 코드
Language = str | Callable[[Any, float, float], str]


class Engine(ABC):
    name: str

    @abstractmethod
    def transcribe(self, wav: Path, language: Language, regions: list[Region] | None = None) -> list[Segment]:
        """16kHz mono wav → 단어 타임스탬프가 붙은 segments (speaker는 비어 있어도 됨).

        regions가 주어지면 그 구간만 전사한다(엔진이 지원하지 않으면 무시해도 됨).
        language가 함수면 청크마다 불러 그 청크의 언어를 정한다(`--mixed-language per-region`).
        """

    def language_probs(self, clip) -> dict[str, float]:
        """창(30초 이하)의 언어별 확률. `--mixed-language per-region`용."""
        raise NotImplementedError(f"{self.name} 엔진은 구간별 언어 판별을 지원하지 않음")

    def detect_language(self, clip) -> tuple[dict[str, float], str]:
        """16kHz mono 창(30초 이하) → (언어별 확률, 언어를 정하지 않고 받아 적은 글자). `--language auto`용."""
        raise NotImplementedError(f"{self.name} 엔진은 --language auto를 지원하지 않음")
