from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..segments import Segment


class Engine(ABC):
    name: str

    @abstractmethod
    def transcribe(self, wav: Path, language: str) -> list[Segment]:
        """16kHz mono wav → 단어 타임스탬프가 붙은 segments (speaker는 비어 있어도 됨)."""
