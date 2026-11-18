"""rec2md changes — review로 고친 곳(문장·화자·메모)을 기준 시점 이후만 뽑는다.

  rec2md changes <이름>.segments.json                 # 기준: <이름>.녹취록.md 수정시각(없으면 전체)
  rec2md changes <이름>.segments.json --since 2026-11-18T14:00
  rec2md changes <이름>.segments.json --since <파일>   # 그 파일의 수정시각
  rec2md changes <이름>.segments.json --json

녹취록.md는 고쳐 쓰지 않는다. 에이전트가 출력의 시각을 보고 그 문단만 고친다(SKILL.md 2단계).
메모가 바뀌었으면 지난번 실행 때 본 메모(<이름>.memo.seen.md)와의 diff를 보이고 그 사본을 갱신한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .changes import changes, parse_since, render_text


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rec2md changes", description=__doc__.split("\n\n")[0])
    ap.add_argument("segments", type=Path, help="<이름>.segments.json")
    ap.add_argument("--since", help="기준 시점: 파일 경로(수정시각) 또는 ISO 시각. 기본은 <이름>.녹취록.md 수정시각")
    ap.add_argument("--json", action="store_true", help="JSON으로 출력")
    ap.add_argument("--no-mark", action="store_true", help="메모 사본(.memo.seen.md)을 갱신하지 않는다")
    a = ap.parse_args(argv)
    if not a.segments.is_file():
        raise SystemExit(f"파일 없음: {a.segments}")
    c = changes(a.segments, parse_since(a.since, a.segments), update_seen=not a.no_mark)
    if a.json:
        json.dump(c.to_dict(), sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(c, a.segments))
