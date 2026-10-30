"""rec2md notes — 회의 메모를 녹취와 정렬한다.

  rec2md notes <이름>.segments.json <메모.md|txt|docx|hwpx> [--series <이름>] [--to-series <이름>]

→ <이름>.notes-align.json (메모 문단별 녹취 근거 후보, 녹취에 없는 숫자·용어, 메모가 안 다룬 녹취 구간).
대조와 보정본 작성은 에이전트가 한다 (SKILL.md "4. 회의 메모 보정").
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import notes
from .series import DOC_EXTS, load_series, series_root


def log(msg: str) -> None:
    print(f"[rec2md] {msg}", file=sys.stderr, flush=True)


def stem_of(seg: Path) -> str:
    return seg.name[: -len(".segments.json")] if seg.name.endswith(".segments.json") else seg.stem


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md notes", description="회의 메모 문단마다 녹취 근거 구간 후보를 붙인다")
    p.add_argument("segments", type=Path, help="<이름>.segments.json")
    p.add_argument("memo", type=Path, help=f"회의 메모·회의록 초안 ({', '.join(sorted(DOC_EXTS - {'.pdf'}))})")
    p.add_argument("--series", help="시리즈 참석자·용어를 인명·용어 가중에 쓴다")
    p.add_argument("--to-series", metavar="이름", help="메모를 시리즈 자료(docs/)로 넣어 용어 후보(출처 doc)로 쓴다")
    p.add_argument("--top", type=int, default=notes.TOP_K, help=f"문단마다 근거 후보 수 (기본 {notes.TOP_K})")
    p.add_argument("--window", type=float, default=notes.WINDOW_SEC, help=f"녹취 창 길이(초, 기본 {notes.WINDOW_SEC:g})")
    p.add_argument("--out", type=Path, help="출력 폴더 (기본: segments.json 옆)")
    args = p.parse_args(argv)

    for f in (args.segments, args.memo):
        if not f.is_file():
            raise SystemExit(f"파일 없음: {f}")
    if args.memo.suffix.lower() not in DOC_EXTS - {".pdf"}:
        raise SystemExit(f"메모 형식이 아님: {args.memo.name} (md, txt, docx, hwpx)")

    series_dir = None
    if args.to_series:
        series_dir = series_root() / args.to_series
        if not series_dir.is_dir():
            raise SystemExit(f"시리즈 없음: {args.to_series} (rec2md series init {args.to_series})")

    names: set[str] = set()
    if args.series:
        ctx = load_series(args.series)
        names = set(ctx.attendees) | set(ctx.terms)

    out_dir = args.out or args.segments.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{stem_of(args.segments)}.notes-align.json"
    data = notes.write_align(args.memo, args.segments, out, names, args.top, args.window)
    paras = data["paragraphs"]
    log(f"메모 {len(paras)}문단 중 근거 후보 있음 {sum(bool(x['candidates']) for x in paras)}, "
        f"녹취에 없는 숫자·용어가 있는 문단 {sum(bool(x['missing']) for x in paras)}, "
        f"메모가 안 다룬 녹취 구간 {len(data['uncovered'])}")
    log(f"→ {out}")

    if series_dir:
        (series_dir / "docs").mkdir(exist_ok=True)
        shutil.copy2(args.memo, series_dir / "docs" / args.memo.name)
        log(f"시리즈 '{args.to_series}' 자료로 복사: {args.memo.name} (용어 후보는 rec2md series show {args.to_series})")
