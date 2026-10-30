"""클로바노트 txt 내보내기 → Transcript (품질 비교용 기준점).

형식:
    <제목>
    2026.10.16 Fri AM 10:50 ・ 9Minutes 1seconds
    <참석자 목록>

    <화자> 00:00
    <발화(여러 줄 가능)>

클로바 txt에는 발화 시작 시각만 있으므로 끝 시각은 다음 발화 시작으로 채운다.
"""

from __future__ import annotations

import re
from pathlib import Path

from .segments import Segment, Transcript

_HEAD = re.compile(r"^(?P<spk>.+?) (?P<ts>\d{1,2}:\d{2}(?::\d{2})?)$")
_DUR = re.compile(r"(\d+)\s*(Hours?|시간|Minutes?|분|seconds?|초)", re.I)
_UNIT = {"h": 3600, "시": 3600, "m": 60, "분": 60, "s": 1, "초": 1}


def _sec(ts: str) -> float:
    parts = [int(x) for x in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def parse(path: Path) -> Transcript:
    lines = path.read_text(encoding="utf-8-sig").replace("\r", "").split("\n")
    title = lines[0].strip()
    # "・ 9Minutes 1seconds", "・ 17분 34초", "・ 1Hours 5Minutes" 등 일부 단위가 빠질 수 있다
    tail = lines[1].split("・")[-1] if len(lines) > 1 else ""
    duration = float(sum(int(n) * _UNIT[u[0].lower()] for n, u in _DUR.findall(tail)))

    segs: list[Segment] = []
    cur: Segment | None = None
    prev_blank = True
    for line in lines[3:]:
        stripped = line.strip()
        if not stripped:
            prev_blank = True
            continue
        h = _HEAD.match(stripped) if prev_blank else None
        if h:
            cur = Segment(start=_sec(h["ts"]), end=0.0, text="", speaker=h["spk"])
            segs.append(cur)
        elif cur is not None and stripped:
            cur.text = f"{cur.text} {stripped}".strip()
        prev_blank = False
    for a, b in zip(segs, segs[1:]):
        a.end = b.start
    if segs:
        duration = duration or segs[-1].start  # 길이를 못 읽으면 마지막 발화 시각으로 대체
        segs[-1].end = max(duration, segs[-1].start)
    return Transcript(source=path.name, duration=duration, engine="clova-note", language="ko",
                      segments=segs, meta={"title": title})
