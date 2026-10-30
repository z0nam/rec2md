"""rec2md bounds <segments.json...> [--write] — 회의 시작·끝 추정(녹음 앞뒤 잡담·통화 구간).

전사할 때도 자동으로 meta["meeting_bounds"]에 남는다. 이 명령은 이미 전사한 결과에 다시 돌리거나
근거를 보려고 쓴다. --write를 주면 segments.json의 meta를 갱신한다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .bounds import estimate, summary
from .segments import Transcript


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md bounds", description="회의 시작·끝 추정")
    p.add_argument("segments", nargs="+", type=Path)
    p.add_argument("--write", action="store_true", help="segments.json meta.meeting_bounds 갱신")
    args = p.parse_args(argv)
    for path in args.segments:
        t = Transcript.from_json(path)
        b = estimate(t)
        print(f"{path.name}: {summary(b, t.duration)}")
        for e in b["evidence"]:
            print(f"  - {e}")
        if args.write:
            t.meta["meeting_bounds"] = b
            t.to_json(path)
