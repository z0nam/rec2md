"""rec2md ingest <녹음기 폴더> <보관 폴더>: 녹음기의 새 녹음을 보관 폴더로 받고, 원하면 전사·녹음기 정리까지.

  rec2md ingest E:\\REC_DIR D:\\recordings --dry-run                       # 무엇을 받을지(쓰지 않음)
  rec2md ingest E:\\REC_DIR D:\\recordings                                 # ① 새 파일만 복사 + sha1 확인
  rec2md ingest E:\\REC_DIR D:\\recordings --batch -- --remote studio      # ② 이어서 보관 폴더를 batch로 전사
  rec2md ingest E:\\REC_DIR D:\\recordings --move-processed E:\\처리완료    # ③ 끝난 것만 녹음기 안에서 옮김

- ① 녹음기 폴더 바로 아래 녹음 파일 중 매니페스트(<보관 폴더>/.rec2md-ingest.json)에 없는 것만 복사한다.
  녹음은 이름이 아니라 지문(크기 + sha1)으로 구분한다 — 녹음기는 파일을 정리하면 번호를 되감는다(이슈 #40).
  같은 이름이 보관 폴더에 있으면 덮어쓰지 않고 `-2` 같은 접미사를 붙인다.
- ② `--batch` 뒤 `--` 다음의 인자는 그대로 `rec2md batch <보관 폴더>`에 넘긴다(`--out`은 이 명령의 것을 쓴다).
- ③ `--move-processed <폴더>`: 보관본이 있고, 전사 상태가 done이고 segments.json이 있고, 녹음기 파일의 sha1이 보관본과
  같은 파일만 그 폴더로 옮긴다. 폴더는 녹음기와 같은 디스크여야 한다(이름 바꾸기라 되돌리기 쉽다).
  지정하지 않으면 녹음기에는 아무것도 쓰지 않는다. 지우는 기능은 없다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import ingest as ig
from .cli import log


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rec2md ingest", description="녹음기의 새 녹음을 보관 폴더로 받기 (+ 전사, 녹음기 정리)")
    p.add_argument("recorder", type=Path, help="녹음기 녹음 폴더 (예: E:\\REC_DIR, 하위 폴더는 보지 않음)")
    p.add_argument("archive", type=Path, help="보관 폴더 (없으면 만듦)")
    p.add_argument("--dry-run", action="store_true", help="받을 것·옮길 것만 보여 주고 아무것도 쓰지 않음")
    p.add_argument("--batch", action="store_true",
                   help="받은 뒤 보관 폴더를 rec2md batch로 전사. batch 옵션은 맨 뒤 `--` 다음에")
    p.add_argument("--out", type=Path, help="전사 결과 폴더 (기본: <보관 폴더>/_transcripts, batch와 같음)")
    p.add_argument("--move-processed", type=Path, metavar="폴더",
                   help="전사가 끝나고 sha1이 맞는 녹음기 파일을 이 폴더로 옮김 (녹음기와 같은 디스크)")
    return p


def main(argv: list[str]) -> None:
    batch_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, batch_args = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    if batch_args and not args.batch:
        raise SystemExit("`--` 뒤 인자는 --batch와 함께만 쓴다")
    rec, arc = args.recorder, args.archive
    if not rec.is_dir():
        raise SystemExit(f"녹음기 폴더 없음: {rec}")
    if arc.exists() and not arc.is_dir():
        raise SystemExit(f"보관 폴더가 파일임: {arc}")
    if arc.is_dir() and rec.resolve() == arc.resolve():
        raise SystemExit("녹음기 폴더와 보관 폴더가 같음")
    out = args.out or arc / "_transcripts"
    dest_dir = args.move_processed
    if dest_dir is not None:
        _check_dest(rec, dest_dir)

    manifest = ig.load_manifest(arc) if arc.is_dir() else {"version": 1, "files": {}}
    entries = ig.plan(rec, arc, manifest)
    if arc.is_dir():
        ig.adopt_existing(entries, arc, manifest)  # 보관 디스크만 읽는다
        if not args.dry_run:
            ig.save_manifest(arc, manifest)
    print(report(entries))
    if not entries:
        return

    if args.dry_run:
        if dest_dir is not None:
            print(move_report(entries, arc, manifest, out, dest_dir))
        if args.batch:
            print(f"\n받은 뒤 실행할 batch: rec2md batch {arc} --out {out} {' '.join(batch_args)}".rstrip())
        return

    arc.mkdir(parents=True, exist_ok=True)
    failed = _copy_all(entries, arc, manifest)
    batch_error = _run_batch(arc, out, batch_args) if args.batch else None
    moved, skipped = _move_all(entries, arc, manifest, out, dest_dir) if dest_dir is not None else (0, [])

    log(f"끝: 받음 {sum(1 for e in entries if e.kind == ig.KNOWN and e.sha1)}개, 이미 받음 "
        f"{sum(1 for e in entries if e.kind == ig.KNOWN and not e.sha1)}개, 실패 {len(failed)}개"
        + (f", 녹음기에서 옮김 {moved}개" if dest_dir is not None else "") + f" → {arc}")
    problems = [f"{e.src.name}: {e.error}" for e in failed]
    if batch_error:
        problems.append(f"batch: {batch_error}")
    problems += [f"옮기지 못함 {name}: {why}" for name, why in skipped if why.startswith("오류")]
    if problems:
        raise SystemExit("문제:\n  " + "\n  ".join(problems))


def _check_dest(rec: Path, dest_dir: Path) -> None:
    """처리완료 폴더는 녹음기와 같은 디스크여야 하고, 녹음 폴더 자체이면 안 된다."""
    probe = dest_dir if dest_dir.exists() else dest_dir.parent
    if dest_dir.exists() and not dest_dir.is_dir():
        raise SystemExit(f"--move-processed가 파일임: {dest_dir}")
    if not probe.is_dir():
        raise SystemExit(f"--move-processed 상위 폴더 없음: {probe}")
    if dest_dir.exists() and dest_dir.resolve() == rec.resolve():
        raise SystemExit("--move-processed가 녹음기 폴더와 같음")
    if not ig.same_device(rec, probe):
        raise SystemExit(f"--move-processed는 녹음기와 같은 디스크여야 함(복사+삭제가 아니라 이름 바꾸기로 옮김): {dest_dir}")


def report(entries: list[ig.Entry]) -> str:
    if not entries:
        return "녹음기 폴더에 녹음 파일이 없음"
    w = max(len(e.src.name) for e in entries)
    lines = [f"{'녹음기 파일':<{w}}  {'크기':>8}  판정 → 보관 이름", "-" * (w + 40)]
    for e in entries:
        note = f"  ({'; '.join(e.notes)})" if e.notes else ""
        lines.append(f"{e.src.name:<{w}}  {e.size / 2**20:7.1f}M  {e.kind} → {e.dest}{note}")
    new = [e for e in entries if e.kind in ig.COPY]
    mb = sum(e.size for e in new) / 2**20
    lines += ["", (f"새로 받을 것 {len(new)}개 · {mb:.0f}MB · 녹음기 0.5MB/s면 약 {mb / 0.5 / 60:.0f}분"
                   f" · 이미 받음 {len(entries) - len(new)}개")]
    return "\n".join(lines)


def move_report(entries: list[ig.Entry], arc: Path, manifest: dict, out: Path, dest_dir: Path) -> str:
    lines = ["", f"--move-processed {dest_dir} (sha1은 실제 실행 때 확인)"]
    for e in entries:
        why = ig.move_check(e, arc, manifest, out, dest_dir)
        if e.kind != ig.KNOWN:
            why = "새 파일 — 받은 뒤 전사가 끝나야 옮김"
        lines.append(f"  {e.src.name}: {'옮길 후보' if why is None else '그대로 — ' + why}")
    return "\n".join(lines)


def _copy_all(entries: list[ig.Entry], arc: Path, manifest: dict) -> list[ig.Entry]:
    todo = [e for e in entries if e.kind in ig.COPY]
    if not todo:
        return []
    problem = ig.free_space_problem(entries, arc)
    if problem:
        raise SystemExit(problem)
    failed = []
    for n, e in enumerate(todo, 1):
        log(f"[{n}/{len(todo)}] {e.src.name} → {e.dest} ({e.size / 2**20:.0f}MB)")
        try:
            ig.copy_in(e, arc, manifest)
        except KeyboardInterrupt:
            raise
        except Exception as ex:  # 한 파일이 실패해도 다음으로
            e.error = f"{type(ex).__name__}: {ex}"
            failed.append(e)
            log(f"실패: {e.src.name} — {e.error}")
            continue
        e.kind = ig.KNOWN
        ig.save_manifest(arc, manifest)
    return failed


def _run_batch(arc: Path, out: Path, batch_args: list[str]) -> str | None:
    """batch는 실패가 있으면 SystemExit로 끝난다. 정리 단계는 파일별로 done만 보므로 이어서 진행한다."""
    from . import cmd_batch
    log(f"batch: {arc} → {out}")
    try:
        cmd_batch.main([str(arc), "--out", str(out), *batch_args])
    except SystemExit as ex:
        if ex.code not in (None, 0):
            return str(ex.code)
    return None


def _move_all(entries: list[ig.Entry], arc: Path, manifest: dict, out: Path, dest_dir: Path) -> tuple[int, list]:
    dest_dir.mkdir(exist_ok=True)
    moved, skipped = 0, []
    for e in entries:
        why = ig.move_check(e, arc, manifest, out, dest_dir)
        if why:
            skipped.append((e.src.name, why))
            log(f"그대로 둠: {e.src.name} — {why}")
            continue
        try:
            ig.move_out(e, arc, manifest, dest_dir)
        except Exception as ex:
            skipped.append((e.src.name, f"오류 {type(ex).__name__}: {ex}"))
            log(f"옮기지 못함: {e.src.name} — {ex}")
            continue
        moved += 1
        ig.save_manifest(arc, manifest)
        log(f"옮김: {e.src.name} → {dest_dir}")
    return moved, skipped
