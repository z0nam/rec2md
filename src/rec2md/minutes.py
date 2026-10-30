"""회의록 공통 스키마. 에이전트가 <이름>.minutes.json으로 쓰고, 렌더러(md/html/docx/hwpx)는 이것만 본다.

필드 설명과 예시는 docs/minutes-schema.md.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass
class Attendee:
    name: str
    org: str = ""   # 소속 (예: 제주연구원, 도청 ○○과)
    role: str = ""  # 직위·역할 (예: 연구위원, 간사)


@dataclass
class Item:
    """회의내용의 한 줄. children으로 한 단계 더 내려간다(렌더러는 3단까지 보장)."""
    text: str
    children: list[Item] = field(default_factory=list)
    ts: float | None = None  # 근거 발언 시각(초). 렌더러는 옵션으로만 표시


@dataclass
class Section:
    heading: str                 # 예: "비전·전략 확정 방식"
    items: list[Item] = field(default_factory=list)


@dataclass
class Action:
    task: str
    owner: str = ""
    due: str = ""  # 자유 형식 문자열 ("11/5", "2026-11-05", "수시")


@dataclass
class Minutes:
    title: str
    date: str = ""                # "2026-10-29"
    time: str = ""                # "10:00-11:30"
    place: str = ""
    agenda: list[str] = field(default_factory=list)
    attendees: list[Attendee] = field(default_factory=list)
    author: str = ""              # 작성자
    owner: str = ""               # 연구자·주관자 (양식에 따라 쓰임)
    summary: list[str] = field(default_factory=list)   # 개요 bullet
    sections: list[Section] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    next_meeting: str = ""
    attachments: list[str] = field(default_factory=list)
    source: dict = field(default_factory=dict)  # {"recording", "transcript", "range": [start, end]}
    version: int = SCHEMA_VERSION

    def to_json(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def from_dict(cls, d: dict) -> Minutes:
        def item(x: dict) -> Item:
            return Item(text=x["text"], ts=x.get("ts"), children=[item(c) for c in x.get("children", [])])

        d = dict(d)
        d["attendees"] = [Attendee(**a) for a in d.get("attendees", [])]
        d["sections"] = [Section(heading=s["heading"], items=[item(i) for i in s.get("items", [])])
                         for s in d.get("sections", [])]
        d["actions"] = [Action(**a) for a in d.get("actions", [])]
        return cls(**d)

    @classmethod
    def from_json(cls, path: Path) -> Minutes:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
