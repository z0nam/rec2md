"""검토 화면(rec2md review)의 데이터 쪽: 편집 로그 적용, 저장, 판정 사례·답 파일.

편집 로그 `<이름>.edits.json`은 원본(`<이름>.segments.orig.json`)에 차례로 적용하는 연산 목록이다.
`apply_edits(원본, 로그)`는 순수 함수라 같은 로그를 다시 적용하면 같은 결과가 나온다. 화면은 편집 한 건마다
서버에 보내고, 서버는 로그에 덧붙인 뒤 원본에서 다시 적용해 segments.json·raw.md를 고쳐 쓴다.
되돌리기는 로그의 마지막 연산을 빼고 다시 적용하는 것이다.

연산
- `{"op": "set_speaker", "scope": "one"|"after"|"all", "seg": i, "from": "화자3", "to": "화자1" | {"name": "홍길동"}}`
  - one: 세그먼트 i만, after: i부터 끝까지 라벨이 `from`인 것, all: 라벨이 `from`인 것 전부
  - `to`가 라벨이면 그 라벨로 옮긴다. 이름이면, 그 이름을 사용자가 이미 붙인 라벨이 있으면 그 라벨로 옮기고,
    없으면 all은 `from` 라벨에 이름을 붙이고, one·after는 새 라벨 `화자N`을 만들어 이름을 붙인다.
  - 사용자가 붙인 이름은 `speakers.user_entry()`로 넣는다. 목소리 DB(`rec2md voices name`,
    `enroll --from-names`)와 같은 계약이고 정의는 speakers.py 한 곳에 있다.
- `{"op": "set_text", "seg": i, "from": "원문", "to": "수정문"}`
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from .render import render_raw
from .segments import Transcript
from .speakers import display_name, is_user, user_entry, user_label_for

SCOPES = ("one", "after", "all")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---- 편집 적용 ----

def _label_num(label: str) -> int | None:
    m = re.fullmatch(r"화자\s*(\d+)", label or "")
    return int(m.group(1)) if m else None


def new_label(t: Transcript) -> str:
    used = {s.speaker for s in t.segments if s.speaker} | set(t.meta.get("speaker_names") or {})
    n = max((x for x in map(_label_num, used) if x is not None), default=0) + 1
    while f"화자{n}" in used:
        n += 1
    return f"화자{n}"


def targets(t: Transcript, scope: str, seg: int, label: str | None) -> list[int]:
    """연산이 바꿀 세그먼트 번호. 화면의 "영향받는 구간 수" 미리보기와 같은 규칙."""
    if scope == "one":
        return [seg]
    start = seg if scope == "after" else 0
    return [j for j in range(start, len(t.segments)) if t.segments[j].speaker == label]


def _set_speaker(t: Transcript, e: dict) -> None:
    scope, i = e["scope"], int(e["seg"])
    if scope not in SCOPES:
        raise ValueError(f"scope는 {', '.join(SCOPES)}: {scope}")
    if not 0 <= i < len(t.segments):
        raise ValueError(f"세그먼트 번호 범위 밖: {i}")
    src = e.get("from", t.segments[i].speaker)
    names = t.meta.setdefault("speaker_names", {})
    to = e["to"]
    if isinstance(to, dict):
        name = str(to.get("name", "")).strip()
        if not name:
            raise ValueError("새 이름이 비어 있음")
        dst = user_label_for(names, name)
        if dst is None:
            if scope == "all" and src:  # 라벨 전체에 이름을 붙이는 것과 같다
                names[src] = user_entry(name)
                return
            dst = new_label(t)
            names[dst] = user_entry(name)
    else:
        dst = str(to)
    for j in targets(t, scope, i, src):
        t.segments[j].speaker = dst


def apply_edits(t: Transcript, edits: list[dict]) -> Transcript:
    """원본 t에 편집 로그를 차례로 적용한 새 Transcript. t는 바꾸지 않는다."""
    out = copy.deepcopy(t)
    for e in edits:
        if e.get("op") == "set_speaker":
            _set_speaker(out, e)
        elif e.get("op") == "set_text":
            out.segments[int(e["seg"])].text = e["to"]
        else:
            raise ValueError(f"모르는 편집 연산: {e.get('op')}")
    # 더는 어느 세그먼트도 쓰지 않는 라벨의 이름 항목은 지운다(목소리 DB 등록 대상에서 빠지게)
    used = {s.speaker for s in out.segments}
    if out.meta.get("speaker_names"):
        out.meta["speaker_names"] = {k: v for k, v in out.meta["speaker_names"].items() if k in used}
    return out


# ---- 파일 ----

class Session:
    """segments.json 하나에 대한 검토 상태. 원본·편집 로그·결과 파일 경로를 쥔다."""

    def __init__(self, seg_path: Path):
        self.seg = seg_path
        stem = seg_path.name[: -len(".segments.json")] if seg_path.name.endswith(".segments.json") else seg_path.stem
        self.stem = stem
        self.orig = seg_path.with_name(f"{stem}.segments.orig.json")
        self.edits_path = seg_path.with_name(f"{stem}.edits.json")
        self.raw = seg_path.with_name(f"{stem}.raw.md")
        self.edits: list[dict] = (json.loads(self.edits_path.read_text(encoding="utf-8"))
                                  if self.edits_path.is_file() else [])
        self.current = self.base()
        if self.edits:
            self.current = apply_edits(self.current, self.edits)

    def base(self) -> Transcript:
        return Transcript.from_json(self.orig if self.orig.is_file() else self.seg)

    def _write(self) -> None:
        if not self.orig.is_file():  # 처음 저장할 때 한 번만 원본을 남긴다
            shutil.copy2(self.seg, self.orig)
        self.current = apply_edits(self.base(), self.edits)
        self.current.to_json(self.seg)
        self.raw.write_text(render_raw(self.current, self.stem), encoding="utf-8")
        self.edits_path.write_text(json.dumps(self.edits, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, edit: dict) -> None:
        edit = {**edit, "at": edit.get("at") or now()}
        apply_edits(self.current, [edit])  # 잘못된 연산이면 로그에 넣기 전에 ValueError
        self.edits.append(edit)
        self._write()

    def undo(self) -> dict | None:
        if not self.edits:
            return None
        e = self.edits.pop()
        self._write()
        return e

    def state(self) -> dict:
        t, names = self.current, self.current.meta.get("speaker_names") or {}
        labels = list(dict.fromkeys(s.speaker for s in t.segments if s.speaker))
        return {
            "mode": "transcript",
            "title": t.meta.get("title") or self.stem,
            "source": t.source, "duration": t.duration,
            "date": t.meta.get("date") or t.meta.get("datetime") or "",
            "segments": [{"i": i, "start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker,
                          "name": display_name(s.speaker, names)} for i, s in enumerate(t.segments)],
            "speakers": [{"label": lb, "name": display_name(lb, names),
                          "user": is_user(names.get(lb))}
                         for lb in labels],
            "edits": len(self.edits),
        }


# ---- 판정 모드 ----

def answers_path(cases_path: Path) -> Path:
    n = cases_path.name
    stem = n[: -len(".cases.json")] if n.endswith(".cases.json") else cases_path.stem
    return cases_path.with_name(f"{stem}.answers.json")


class Queue:
    def __init__(self, cases_path: Path):
        self.path = cases_path
        self.data = json.loads(cases_path.read_text(encoding="utf-8"))
        ids = [c.get("id") for c in self.data.get("cases", [])]
        if len(set(ids)) != len(ids) or None in ids:
            raise ValueError("cases.json의 id가 비었거나 겹침")
        self.answers_path = answers_path(cases_path)
        self.answers: list[dict] = (json.loads(self.answers_path.read_text(encoding="utf-8"))
                                    if self.answers_path.is_file() else [])

    def answer(self, case_id: str, key: str | None, text: str | None, unsure: bool) -> dict:
        case = next((c for c in self.data["cases"] if c["id"] == case_id), None)
        if case is None:
            raise ValueError(f"없는 사례: {case_id}")
        keys = {o["key"] for o in case.get("options", [])}
        if key is not None and key not in keys:
            raise ValueError(f"사례 {case_id}에 없는 선택지: {key}")
        if text and not case.get("allow_text"):
            raise ValueError(f"사례 {case_id}는 직접 입력을 받지 않음")
        if unsure:
            key, text = None, None
        a = {"id": case_id, "key": key, "text": text or None, "unsure": bool(unsure), "at": now()}
        # 같은 사례에 다시 답하면 덮어쓴다(사례 순서 유지)
        self.answers = [x for x in self.answers if x["id"] != case_id] + [a]
        order = {c["id"]: n for n, c in enumerate(self.data["cases"])}
        self.answers.sort(key=lambda x: order.get(x["id"], len(order)))
        self.answers_path.write_text(json.dumps(self.answers, ensure_ascii=False, indent=1), encoding="utf-8")
        return a

    def state(self) -> dict:
        return {"mode": "queue", "title": self.path.name, "cases": self.data.get("cases", []),
                "answers": {a["id"]: a for a in self.answers}}
