"""review 편집 이후 바뀐 곳 뽑기(`rec2md changes`). 녹취록.md를 고쳐 쓰지 않고, 에이전트가 고칠 문단만 알려 준다.

기준 시점(`since`)의 녹취와 지금 녹취를 세그먼트별로 비교한다.
- 기준 시점의 녹취 = 원본(`<이름>.segments.orig.json`)에 편집 로그 중 `at`이 기준 시점 이전인 앞부분만 적용한 것.
  `at`이 없는 예전 로그 항목은 "시각 불명"이라 기준 시점 이후로 본다(빠뜨리는 것보다 한 번 더 보는 편이 낫다).
- 지금 녹취 = 로그 전체를 적용한 것(= review가 쓴 segments.json).
- 문장이 바뀐 세그먼트는 하나씩 보인다(시각, 화자 전→후, 문장 전→후).
- 문장은 그대로이고 화자 이름만 바뀐 세그먼트는 (전 이름, 후 이름) 쌍으로 묶어 한 줄로 보인다
  ("화자3 → 홍길동 (전체 12구간)"). 그 쌍의 전 이름을 쓰던 구간이 모두 바뀌었으면 "전체", 일부면 "N구간".
- 메모(`<이름>.memo.md`)가 기준 시점 이후 바뀌었으면 알리고, 지난번 `rec2md changes` 때 본 메모
  (`<이름>.memo.seen.md`)와의 diff를 보인다. 그 사본은 실행할 때마다 지금 메모로 갱신한다.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .render import fmt_ts
from .review import apply_edits, side_file
from .segments import Transcript
from .speakers import display_name


def stem_of(seg: Path) -> str:
    return seg.name[: -len(".segments.json")] if seg.name.endswith(".segments.json") else seg.stem


def since_of(p: Path) -> datetime:
    return datetime.fromtimestamp(p.stat().st_mtime).astimezone()


def parse_since(value: str | None, seg: Path) -> datetime | None:
    """--since: 파일이면 그 수정시각, 아니면 ISO 시각. 없으면 `<이름>.녹취록.md`의 수정시각, 그것도 없으면 None(전체)."""
    if value is None:
        p = side_file(seg, stem_of(seg), ".녹취록.md")
        return since_of(p) if p.is_file() else None
    p = Path(value)
    if p.is_file():
        return since_of(p)
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"--since는 파일 경로나 ISO 시각(예: 2026-11-18T14:00): {value}") from None
    return t if t.tzinfo else t.astimezone()


def _at(e: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(e["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


@dataclass
class Changes:
    since: datetime | None
    edits_total: int = 0
    edits_after: int = 0         # 기준 시점 이후(시각 불명 포함) 편집 수
    unknown_time: int = 0        # 그중 시각 불명
    texts: list[dict] = field(default_factory=list)     # 문장이 바뀐 세그먼트
    speakers: list[dict] = field(default_factory=list)  # 화자만 바뀐 묶음
    memo: dict | None = None
    notice: str = ""

    def count(self) -> int:
        return len(self.texts) + len(self.speakers)

    def to_dict(self) -> dict:
        return {"since": self.since.isoformat(timespec="seconds") if self.since else None,
                "edits_total": self.edits_total, "edits_after": self.edits_after, "unknown_time": self.unknown_time,
                "count": self.count(), "texts": self.texts, "speakers": self.speakers, "memo": self.memo,
                "notice": self.notice}


def _edits(seg: Path) -> list[dict]:
    p = side_file(seg, stem_of(seg), ".edits.json")
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []


def split_at(edits: list[dict], since: datetime | None) -> int:
    """기준 시점까지 적용된 편집 수. 로그는 덧붙이기만 하므로 앞부분이다. 시각 불명이 나오면 거기서 끊는다."""
    if since is None:
        return 0
    n = 0
    for e in edits:
        t = _at(e)
        if t is None or t > since:
            break
        n += 1
    return n


def diff_transcripts(before: Transcript, after: Transcript) -> tuple[list[dict], list[dict]]:
    """세그먼트별 비교 → (문장 변경 목록, 화자 변경 묶음). 편집은 세그먼트를 늘리거나 줄이지 않는다."""
    bn, an = before.meta.get("speaker_names") or {}, after.meta.get("speaker_names") or {}
    texts, groups = [], {}
    left = {display_name(a.speaker, an) for a in after.segments}  # 바뀐 뒤에도 남아 있는 이름
    for i, (b, a) in enumerate(zip(before.segments, after.segments)):
        sb, sa = display_name(b.speaker, bn), display_name(a.speaker, an)
        if b.text != a.text:
            texts.append({"seg": i, "start": a.start, "end": a.end, "ts": fmt_ts(a.start),
                          "speaker_before": sb, "speaker_after": sa, "text_before": b.text, "text_after": a.text})
        elif sb != sa:
            g = groups.setdefault((sb, sa), {"from": sb, "to": sa, "segs": [], "first": a.start})
            g["segs"].append(i)
    speakers = []
    for g in sorted(groups.values(), key=lambda g: g["first"]):
        n = len(g["segs"])
        speakers.append({"from": g["from"], "to": g["to"], "n": n, "all": g["from"] not in left,
                         "first_ts": fmt_ts(g["first"]),
                         "segs": g["segs"]})
    return texts, speakers


def memo_change(seg: Path, since: datetime | None, update_seen: bool = True) -> dict | None:
    stem = stem_of(seg)
    memo, seen = side_file(seg, stem, ".memo.md"), side_file(seg, stem, ".memo.seen.md")
    if not memo.is_file():
        return None
    mtime = datetime.fromtimestamp(memo.stat().st_mtime).astimezone()
    if since is not None and mtime <= since:
        return None
    now_text = memo.read_text(encoding="utf-8")
    old_text = seen.read_text(encoding="utf-8") if seen.is_file() else ""
    diff = "".join(difflib.unified_diff(old_text.splitlines(keepends=True), now_text.splitlines(keepends=True),
                                        fromfile=seen.name if seen.is_file() else "(지난번 본 메모 없음)",
                                        tofile=memo.name))
    if update_seen:
        seen.write_text(now_text, encoding="utf-8")
    return {"name": memo.name, "modified": mtime.isoformat(timespec="seconds"), "diff": diff,
            "seen_before": seen.is_file() and bool(old_text)}


def changes(seg: Path, since: datetime | None, update_seen: bool = True, with_memo: bool = True) -> Changes:
    stem = stem_of(seg)
    edits = _edits(seg)
    c = Changes(since=since, edits_total=len(edits))
    orig = side_file(seg, stem, ".segments.orig.json")
    if edits and orig.is_file():
        base = Transcript.from_json(orig)
        k = split_at(edits, since)
        after_edits = edits[k:]
        c.edits_after = len(after_edits)
        c.unknown_time = sum(1 for e in after_edits if _at(e) is None)
        before, after = apply_edits(base, edits[:k]), apply_edits(base, edits)
        c.texts, c.speakers = diff_transcripts(before, after)
        now = Transcript.from_json(seg)
        if [(s.text, s.speaker) for s in now.segments] != [(s.text, s.speaker) for s in after.segments]:
            c.notice = ("segments.json이 편집 로그 밖에서 바뀌었음(rec2md voices name 등). 여기 결과는 편집 로그 기준이다. "
                        "rec2md review로 한 번 열면 지금 파일을 새 원본으로 삼는다")
    if with_memo:
        c.memo = memo_change(seg, since, update_seen)
    return c


def render_text(c: Changes, seg: Path) -> str:
    lines = [f"기준: {c.since.isoformat(timespec='seconds') if c.since else '처음부터(녹취록.md 없음)'}",
             f"편집 {c.edits_after}건 / 전체 {c.edits_total}건"
             + (f" (시각 불명 {c.unknown_time}건 포함)" if c.unknown_time else "")
             + f" → 바뀐 곳 {c.count()}건"]
    if c.notice:
        lines.append(f"주의: {c.notice}")
    if c.speakers:
        lines += ["", "## 화자 이름"]
        for g in c.speakers:
            scope = f"전체 {g['n']}구간" if g["all"] else f"{g['n']}구간, 처음 [{g['first_ts']}]"
            lines.append(f"- {g['from']} → {g['to']} ({scope})")
    if c.texts:
        lines += ["", "## 문장"]
        for t in c.texts:
            who = t["speaker_after"] if t["speaker_before"] == t["speaker_after"] \
                else f"{t['speaker_before']} → {t['speaker_after']}"
            lines += [f"- [{t['ts']}] {who}", f"  - 전: {t['text_before']}", f"  - 후: {t['text_after']}"]
    if c.memo:
        lines += ["", f"## 메모 ({c.memo['name']}, {c.memo['modified']} 수정)"]
        lines.append(c.memo["diff"].rstrip() or "(지난번 본 메모와 같음)")
    if not c.count() and not c.memo:
        lines += ["", "바뀐 곳 없음"]
    return "\n".join(lines) + "\n"
