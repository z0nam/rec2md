"""엔진 공통 스키마: [{start, end, speaker, text}]."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str
    speaker: str | None = None
    prob: float = 1.0


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    source: str
    duration: float
    engine: str
    language: str
    segments: list[Segment]
    meta: dict = field(default_factory=dict)

    def to_json(self, path: Path) -> None:
        data = asdict(self)
        # 단어 단위는 화자 재할당용 중간물이라 파일에는 남기지 않는다(용량 절약)
        for s in data["segments"]:
            s.pop("words", None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def from_json(cls, path: Path) -> Transcript:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["segments"] = [Segment(**s) for s in data["segments"]]
        return cls(**data)
