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
