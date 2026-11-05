from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..segments import Segment

Region = tuple[float, float]  # 말소리 구간 (start, end), 초


class Engine(ABC):
    name: str

    @abstractmethod
    def transcribe(self, wav: Path, language: str, regions: list[Region] | None = None) -> list[Segment]:
        """16kHz mono wav → 단어 타임스탬프가 붙은 segments (speaker는 비어 있어도 됨).

        regions가 주어지면 그 구간만 전사한다(엔진이 지원하지 않으면 무시해도 됨).
        """

    def detect_language(self, clip) -> tuple[dict[str, float], str]:
        """16kHz mono 창(30초 이하) → (언어별 확률, 언어를 정하지 않고 받아 적은 글자). `--language auto`용."""
        raise NotImplementedError(f"{self.name} 엔진은 --language auto를 지원하지 않음")
