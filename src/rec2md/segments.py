"""엔진 공통 스키마: [{start, end, speaker, text}]."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 이 확률보다 낮은 단어는 보정 후보로 segments.json에 남긴다
LOW_CONF = 0.5

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
    # 낮은 확률 구간 [[start, end, min_prob, text], ...]. 연달아 낮은 단어는 한 구간으로 합친다.
    # 선택 필드다: 엔진이 단어 확률을 주지 않거나 옛 segments.json이면 비어 있다
    low_conf: list[list] = field(default_factory=list)


def low_conf_spans(words: list[Word], threshold: float = LOW_CONF) -> list[list]:
    spans: list[list] = []
    prev_low = False
    for w in words:
        low = w.prob < threshold
        if low and prev_low:
            sp = spans[-1]
            sp[1], sp[2], sp[3] = round(w.end, 2), min(sp[2], round(w.prob, 2)), sp[3] + w.text
        elif low:
            spans.append([round(w.start, 2), round(w.end, 2), round(w.prob, 2), w.text])
        prev_low = low
    for sp in spans:
        sp[3] = sp[3].strip()
    return spans


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
        # 단어 단위는 화자 재할당용 중간물이라 파일에는 남기지 않는다(용량 절약).
        # 대신 보정 후보가 될 낮은 확률 구간만 압축해서 남긴다
        for seg, s in zip(self.segments, data["segments"]):
            s.pop("words", None)
            spans = low_conf_spans(seg.words) if seg.words else seg.low_conf
            if spans:
                s["low_conf"] = spans
            else:
                s.pop("low_conf", None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def from_json(cls, path: Path) -> Transcript:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["segments"] = [Segment(**s) for s in data["segments"]]  # low_conf 없는 옛 파일도 읽힌다
        return cls(**data)
