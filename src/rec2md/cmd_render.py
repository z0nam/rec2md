"""rec2md render — 회의록 JSON 또는 segments.json을 여러 형식으로.

  rec2md render <이름>.minutes.json --template ji --format md,html,docx,hwpx --export pdf,png
  rec2md render <이름>.segments.json --kind transcript --format txt,md,html

형식별 렌더러는 rec2md.render.<형식>.render_<형식>(m, template, out)이다. 녹취록은
rec2md.transcript_export.export_transcript를 부른다.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

MINUTES_FORMATS = ("md", "html", "docx", "hwpx")
TRANSCRIPT_FORMATS = ("txt", "md", "html")  # transcript_export가 지원하는 것

# 형식별로 필요한 extra (렌더러 모듈의 의존성이 없을 때 안내)
EXTRA_HINT = {"md": "render", "html": "render", "docx": "office", "hwpx": "office"}


def log(msg: str) -> None:
    print(f"[rec2md] {msg}", file=sys.stderr, flush=True)


def csv(s: str) -> list[str]:
    return [x.strip().lower() for x in s.split(",") if x.strip()]


def stem_of(src: Path) -> str:
    """meeting.minutes.json → meeting. 이름에 점이 있어도 끝의 알려진 꼬리만 뗀다."""
    name = src.name
    for tail in (".minutes.json", ".segments.json", ".json"):
        if name.endswith(tail):
            return name[: -len(tail)]
    return src.stem


def detect_kind(data: dict) -> str:
    return "transcript" if "segments" in data else "minutes"


def renderer(fmt: str):
    try:
        mod = importlib.import_module(f"rec2md.render.{fmt}")
    except ModuleNotFoundError as e:
        if e.name == f"rec2md.render.{fmt}":
            raise SystemExit(f"{fmt} 회의록 렌더러가 아직 없음 (rec2md.render.{fmt}). "
                             f"가능한 형식: md, html") from None
        raise SystemExit(f"{fmt} 렌더에 필요한 패키지 '{e.name}'가 없음: "
                         f"uv sync --extra {EXTRA_HINT.get(fmt, fmt)}") from None
    fn = getattr(mod, f"render_{fmt}", None)
    if fn is None:
        raise SystemExit(f"rec2md.render.{fmt}에 render_{fmt}()가 없음")
    return fn


def render_minutes(src: Path, data: dict, args) -> list[Path]:
    from .minutes import Minutes
    from .templates import load_template

    fmts = csv(args.format or "md,html")
    if bad := [f for f in fmts if f not in MINUTES_FORMATS]:
        raise SystemExit(f"회의록 형식이 아님: {', '.join(bad)} (가능: {', '.join(MINUTES_FORMATS)})")
    try:
        m = Minutes.from_dict(data)
    except (KeyError, TypeError) as e:
        raise SystemExit(f"회의록 JSON 형식 오류 ({e}): docs/minutes-schema.md 참고") from None
    try:
        t = load_template(args.template)
    except (FileNotFoundError, ValueError, TypeError) as e:
        raise SystemExit(str(e)) from None
    fns = {fmt: renderer(fmt) for fmt in fmts}  # 하나라도 없으면 아무것도 쓰기 전에 멈춘다
    outs = []
    for fmt, fn in fns.items():
        out = args.out_dir / f"{args.name}.{fmt}"
        outs.append(fn(m, t, out))
        log(f"→ {out}")
    return outs


def render_transcript(src: Path, data: dict, args) -> list[Path]:
    from .segments import Transcript

    fmts = csv(args.format or "txt,md")
    if bad := [f for f in fmts if f not in TRANSCRIPT_FORMATS]:
        raise SystemExit(f"녹취록 형식이 아님: {', '.join(bad)} (가능: {', '.join(TRANSCRIPT_FORMATS)})")
    try:
        from .transcript_export import export_transcript
    except ModuleNotFoundError as e:
        if e.name != f"{__package__}.transcript_export":
            raise
        raise SystemExit("녹취록 내보내기(rec2md.transcript_export)가 아직 없음") from None
    t = Transcript.from_json(src)
    names = dict(kv.split("=", 1) for kv in args.names) if args.names else None
    outs = []
    for fmt in fmts:
        out = args.out_dir / f"{args.name}.{fmt}"
        outs.append(export_transcript(t, fmt, out, names=names, title=args.title, recorded_at=args.recorded_at))
        log(f"→ {out}")
    return outs


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md render", description="회의록 JSON·segments.json → md/html/docx/hwpx (+pdf, png)")
    p.add_argument("input", type=Path, help="<이름>.minutes.json 또는 <이름>.segments.json")
    p.add_argument("--kind", choices=["minutes", "transcript"], help="입력 종류 (기본: 내용으로 판단)")
    p.add_argument("--template", default="kr-standard", help="회의록 양식 이름 또는 폴더 (기본 kr-standard). 목록: --list-templates")
    p.add_argument("--format", help="쉼표 구분. 회의록 기본 md,html / 녹취록 기본 txt,md")
    p.add_argument("--export", default="", help="렌더 결과를 더 바꿀 형식: pdf,png (png는 쪽마다 한 장)")
    p.add_argument("--out", type=Path, help="출력 폴더 (기본: 입력 옆)")
    p.add_argument("--name", help="출력 파일 이름 (기본: 입력에서 .minutes.json/.segments.json을 뗀 것)")
    p.add_argument("--title", help="녹취록 제목 (기본: 파일 이름)")
    p.add_argument("--recorded-at", help="녹취록 녹음 일시 (예: '2026.01.15 오후 2:00')")
    p.add_argument("--names", nargs="*", metavar="화자N=이름", help="녹취록 화자 이름 (기본: segments의 speaker_names)")
    p.add_argument("--list-templates", action="store_true", help="쓸 수 있는 양식 목록")
    if "--list-templates" in argv:
        from .templates import available, load_template
        for n in available():
            print(f"{n}\t{load_template(n).description}")
        return
    args = p.parse_args(argv)

    src: Path = args.input
    if not src.is_file():
        raise SystemExit(f"파일 없음: {src}")
    data = json.loads(src.read_text(encoding="utf-8"))
    kind = args.kind or detect_kind(data)
    args.out_dir = args.out or src.parent
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.name = args.name or stem_of(src)

    outs = render_minutes(src, data, args) if kind == "minutes" else render_transcript(src, data, args)

    targets = csv(args.export)
    if targets:
        from .export import ExportError, export
        # pdf·png 원천: html이 있으면 html 하나만, 없으면 렌더한 형식마다
        sources = [o for o in outs if o.suffix == ".html"] or [o for o in outs if o.suffix in (".docx", ".hwpx")]
        if not sources:
            raise SystemExit("pdf·png로 바꿀 원천이 없음: --format에 html(또는 docx, hwpx)을 넣을 것")
        for s in sources:
            try:
                for o in export(s, targets, args.out_dir):
                    log(f"→ {o}")
            except ExportError as e:
                raise SystemExit(str(e)) from None
