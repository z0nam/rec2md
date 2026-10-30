"""rec2md series init|add|show — 회의 시리즈 맥락(용어집·참석자·자료) 관리.

    rec2md series init <이름> [--from-clova <클로바.txt>]
    rec2md series add <이름> [--term T ...] [--fix "오인식 => 정답" ...] [--attendee N ...] [--doc 파일 ...]
    rec2md series show <이름> [--prompt]
    rec2md series list
    rec2md series candidates <이름> <segments.json...>   이미 전사한 결과에서 보정 후보만 다시 뽑기
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .series import PROMPT_TOKENS, build_prompt, load_series, parse_attendees, parse_glossary, series_root


def _append(path: Path, lines: list[str], existing: set[str]) -> int:
    new = [x for x in dict.fromkeys(lines) if x not in existing]
    if new:
        with path.open("a", encoding="utf-8") as f:
            f.writelines(f"{x}\n" for x in new)
    return len(new)


def _dir(name: str) -> Path:
    d = series_root() / name
    if not d.is_dir():
        raise SystemExit(f"시리즈 없음: {name} (rec2md series init {name})")
    return d


def cmd_init(args) -> None:
    d = series_root() / args.name
    (d / "docs").mkdir(parents=True, exist_ok=True)
    for f, head in [("glossary.txt", "# 한 줄에 한 항목. 오인식 표기를 알면 `오인식 => 정답`\n"),
                    ("attendees.txt", "# 참석자. 한 줄에 한 명\n")]:
        if not (d / f).exists():
            (d / f).write_text(head, encoding="utf-8")
    if args.from_clova:
        # 클로바 txt 3번째 줄이 참석자 목록이다
        lines = args.from_clova.read_text(encoding="utf-8-sig").splitlines()
        names = parse_attendees(lines[2]) if len(lines) > 2 else []
        n = _append(d / "attendees.txt", names, set(parse_attendees((d / "attendees.txt").read_text("utf-8"))))
        print(f"참석자 {n}명 추가 (클로바 3번째 줄)")
    print(d)


def cmd_add(args) -> None:
    d = _dir(args.name)
    g = d / "glossary.txt"
    text = g.read_text(encoding="utf-8") if g.exists() else ""
    terms, variants = parse_glossary(text)
    lines = list(args.term or [])
    for fx in args.fix or []:
        if "=>" not in fx:
            raise SystemExit(f"--fix는 '오인식 => 정답' 형식: {fx}")
        wrong, right = (x.strip() for x in fx.split("=>", 1))
        lines.append(f"{wrong} => {right}")
    existing = set(terms) | {f"{w} => {r}" for w, r in variants.items()}
    print(f"용어 {_append(g, lines, existing)}건 추가")
    if args.attendee:
        a = d / "attendees.txt"
        cur = set(parse_attendees(a.read_text(encoding="utf-8"))) if a.exists() else set()
        lines = []
        for x in args.attendee:
            name, _, org = x.partition("=")
            if name.strip() not in cur:
                lines.append(f"{name.strip()}\t{org.strip()}" if org.strip() else name.strip())
        print(f"참석자 {_append(a, lines, set())}명 추가")
    for p in args.doc or []:
        shutil.copy2(p, d / "docs" / p.name)
        print(f"자료 복사: {p.name}")


def cmd_show(args) -> None:
    ctx = load_series(args.name)
    print(f"{_dir(args.name)}")
    print(f"참석자 {len(ctx.attendees)} | 용어 {len(ctx.terms)} (오인식 매핑 {len(ctx.variants)}) | 자료 후보 {len(ctx.doc_terms)}")
    if args.prompt:
        print(f"\n[initial_prompt, 상한 {PROMPT_TOKENS}토큰]\n{build_prompt(ctx)}")
    else:
        for label, xs in [("참석자", ctx.attendees), ("용어", ctx.terms), ("자료 후보", ctx.doc_terms[:30])]:
            if xs:
                print(f"\n{label}: {', '.join(xs)}")


def cmd_list(_args) -> None:
    root = series_root()
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir():
            print(d.name)


def cmd_candidates(args) -> None:
    from .corrections import write_corrections
    from .segments import Transcript
    ctx = load_series(args.name)
    for seg in args.segments:
        out = seg.with_name(seg.name.removesuffix(".segments.json") + ".corrections.json")
        print(f"{out}: 후보 {write_corrections(Transcript.from_json(seg), ctx, out, args.name)}건")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md series", description="회의 시리즈 맥락(용어집·참석자·자료)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="시리즈 폴더 만들기")
    s.add_argument("name")
    s.add_argument("--from-clova", type=Path, help="클로바노트 txt에서 참석자 가져오기")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("add", help="용어·오인식 매핑·참석자·자료 추가")
    s.add_argument("name")
    s.add_argument("--term", action="append", help="정답 표기 (여러 번)")
    s.add_argument("--fix", action="append", help="'오인식 => 정답' (여러 번)")
    s.add_argument("--attendee", action="append", help="참석자 (여러 번). 소속을 붙이려면 '이름=소속'")
    s.add_argument("--doc", action="append", type=Path, help="자료 파일을 docs/로 복사 (md·txt·hwpx·docx·pdf)")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("show", help="내용 보기")
    s.add_argument("name")
    s.add_argument("--prompt", action="store_true", help="전사에 넘길 initial_prompt 미리보기")
    s.set_defaults(fn=cmd_show)
    sub.add_parser("list", help="시리즈 목록").set_defaults(fn=cmd_list)
    s = sub.add_parser("candidates", help="segments.json → corrections.json (재전사 없이)")
    s.add_argument("name")
    s.add_argument("segments", nargs="+", type=Path)
    s.set_defaults(fn=cmd_candidates)
    args = p.parse_args(argv)
    args.fn(args)
