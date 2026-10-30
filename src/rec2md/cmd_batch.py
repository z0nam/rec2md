"""rec2md batch <dir>: 폴더 안의 녹음·영상을 차례로 전사한다.

  rec2md batch <dir> --dry-run              # 파일별 길이, 건너뛸 것, 총 길이, 예상 처리시간
  rec2md batch <dir> [--min-sec 30] [--out <dir>] [전사 옵션 …]

- 대상: <dir> 바로 아래의 mp3·m4a·wav·mp4·mov (대소문자 무시, 하위 폴더는 보지 않음).
- 출력: 기본 <dir>/_transcripts/. 전사 옵션(--engine, --speakers, --series …)은 `rec2md <파일>`과 같다.
- 상태 파일 <out>/.rec2md-state.json에 {파일명: {"status": "done"|"failed", "sec", "source", "error"}}를 남긴다.
  done은 건너뛰고 failed는 다시 시도한다. 파일 하나가 실패해도 다음으로 넘어간다 → 중단 후 같은 명령으로 이어서.
- 출력 이름이 겹치는 파일은 처리하지 않고 "이름 겹침"으로 표시한다: 같은 폴더의 `회의.mp3`·`회의.wav`(출력이
  둘 다 `회의.segments.json`), 또는 같은 출력 폴더를 이미 다른 원본(다른 폴더의 같은 이름 파일)이 쓴 경우.
- 4시간 넘는 파일은 분할이 필요해 이번에는 건너뛰고 "분할 필요"로 표시한다(분할은 별도 작업).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import audio, cli
from .render import fmt_ts

MEDIA_EXTS = {".mp3", ".m4a", ".wav", ".mp4", ".mov"}
STATE_FILE = ".rec2md-state.json"
MAX_SEC = 4 * 3600          # 이보다 길면 분할 필요
LOCAL_SPEED = 12.0          # local 엔진 처리 속도 가정(실시간 대비 배수, 이 맥 실측 근사)

# 계획 단계의 판정
TODO, RETRY, DONE, SHORT, SPLIT, BROKEN = "처리", "재시도", "이미 처리", "짧음", "분할 필요", "길이 확인 실패"
CLASH = "이름 겹침"


@dataclass
class Item:
    path: Path
    sec: float | None
    plan: str


def media_files(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS)


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"상태 파일을 읽을 수 없음: {path} (지우면 처음부터 다시 함)") from None


def save_state(path: Path, state: dict) -> None:
    """중간에 끊겨도 깨진 파일이 남지 않게 임시 파일에 쓰고 바꿔 끼운다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rec2md-state-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _clashes(files: list[Path], state: dict) -> set[Path]:
    """출력 이름(stem)이 겹치는 파일. 대소문자만 다른 이름도 겹친다(맥·윈도 파일시스템)."""
    by_stem: dict[str, list[Path]] = {}
    for f in files:
        by_stem.setdefault(f.stem.casefold(), []).append(f)
    out = {f for group in by_stem.values() if len(group) > 1 for f in group}
    # 같은 출력 폴더를 다른 원본이 이미 썼다: 출력 이름(stem)이 같은 기록이 이 파일 것이 아니면 겹침.
    # 이 파일 것인지는 source 경로로, source가 없는 예전 기록은 파일명으로 판단한다
    for name, rec in state.items():
        for f in by_stem.get(Path(name).stem.casefold(), []):
            if not _same_source(f, name, rec if isinstance(rec, dict) else {}):
                out.add(f)
    return out


def _same_source(f: Path, name: str, rec: dict) -> bool:
    """상태 기록이 이 파일 것인가. 파일명이 같으면 원본 경로가 달라도(폴더를 옮김, 외장 디스크가 다른 경로로
    붙음) 길이가 같을 때 같은 원본으로 본다. 파일명이 다르면(회의.mp3 ↔ 회의.wav) 다른 원본."""
    if name != f.name:
        return False
    src = rec.get("source")
    if not src or src == str(f.resolve()):
        return True
    sec = rec.get("sec")
    if sec is None:
        return False
    try:
        return abs(audio.probe_duration(f) - float(sec)) < 0.5
    except Exception:
        return False


def plan(files: list[Path], state: dict, min_sec: float) -> list[Item]:
    items = []
    clash = _clashes(files, state)
    for f in files:
        if f in clash:
            items.append(Item(f, None, CLASH))
            continue
        try:
            sec = audio.probe_duration(f)
        except Exception:  # ffprobe가 못 읽는 파일도 목록에는 남긴다
            items.append(Item(f, None, BROKEN))
            continue
        st = state.get(f.name, {}).get("status")
        if st == "done":
            kind = DONE
        elif sec < min_sec:
            kind = SHORT
        elif sec > MAX_SEC:
            kind = SPLIT
        else:
            kind = RETRY if st == "failed" else TODO
        items.append(Item(f, sec, kind))
    return items


def report(items: list[Item]) -> str:
    """dry-run 표: 파일별 길이와 판정, 처리 대상 총 길이와 local 예상 처리시간."""
    w = max([len(i.path.name) for i in items] + [4])
    lines = [f"{'파일':<{w}}  {'길이':>8}  판정", "-" * (w + 20)]
    for i in items:
        lines.append(f"{i.path.name:<{w}}  {fmt_ts(i.sec) if i.sec is not None else '-':>8}  {i.plan}")
    todo = [i for i in items if i.plan in (TODO, RETRY)]
    total = sum(i.sec for i in todo)
    counts = {k: sum(1 for i in items if i.plan == k) for k in (TODO, RETRY, DONE, SHORT, SPLIT, BROKEN, CLASH)}
    lines += ["", "  ".join(f"{k} {n}" for k, n in counts.items() if n),
              f"처리 대상 {len(todo)}개 · 총 {fmt_ts(total)} · local 예상 처리시간 약 {fmt_ts(total / LOCAL_SPEED)}"
              f" (실시간 {LOCAL_SPEED:g}배 가정)"]
    if counts[SPLIT]:
        lines.append(f"4시간({fmt_ts(MAX_SEC)})을 넘는 {counts[SPLIT]}개는 분할이 필요해 이번에는 건너뜀")
    if counts[CLASH]:
        lines.append(f"출력 이름이 겹치는 {counts[CLASH]}개는 처리하지 않음 — 파일 이름을 바꾸거나 --out을 따로 지정")
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    p = cli.build_parser(prog="rec2md batch", inputs=False)
    p.description = "폴더 안의 녹음·영상을 차례로 전사 (상태 파일로 이어서)"
    p.add_argument("dir", type=Path, help="녹음 파일이 있는 폴더 (하위 폴더는 보지 않음)")
    p.add_argument("--dry-run", action="store_true", help="파일별 길이·건너뛸 것·예상 처리시간만 보고 끝냄")
    p.add_argument("--min-sec", type=float, default=30.0, help="이보다 짧은 파일은 건너뜀 (기본 30초)")
    args = p.parse_args(argv)

    src_dir: Path = args.dir
    if not src_dir.is_dir():
        raise SystemExit(f"폴더 없음: {src_dir}")
    out = Path(args.out) if args.out else src_dir / "_transcripts"
    args.out = str(out)  # run_one이 이 폴더에 쓴다
    state_path = out / STATE_FILE
    state = load_state(state_path)

    files = media_files(src_dir)
    if not files:
        raise SystemExit(f"처리할 녹음·영상이 없음: {src_dir} ({', '.join(sorted(MEDIA_EXTS))})")
    items = plan(files, state, args.min_sec)
    print(report(items))
    if args.dry_run:
        return

    todo = [i for i in items if i.plan in (TODO, RETRY)]
    failed = 0
    for n, item in enumerate(todo, 1):
        cli.log(f"[{n}/{len(todo)}] {item.path.name}")
        try:
            cli.run_one(item.path, args)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # 한 파일이 실패해도 다음으로
            failed += 1
            state[item.path.name] = {"status": "failed", "sec": item.sec, "source": str(item.path.resolve()),
                                     "error": f"{type(e).__name__}: {e}"}
            cli.log(f"실패: {item.path.name} — {type(e).__name__}: {e}")
        else:
            state[item.path.name] = {"status": "done", "sec": item.sec, "source": str(item.path.resolve())}
        save_state(state_path, state)

    clashed = [i.path.name for i in items if i.plan == CLASH]
    cli.log(f"끝: 처리 {len(todo) - failed}개, 실패 {failed}개, 이름 겹침 {len(clashed)}개 → {out}")
    if failed:
        raise SystemExit(f"실패 {failed}개 — 같은 명령을 다시 실행하면 실패한 것만 다시 시도함 ({state_path})")
    if clashed:
        raise SystemExit(f"출력 이름이 겹쳐 처리하지 않은 파일: {', '.join(clashed)} — 이름을 바꾸거나 --out을 따로 지정. "
                         f"같은 파일인데 겹침으로 나오면 상태 파일({state_path})에서 그 항목을 지우면 된다")
