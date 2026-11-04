"""rec2md index — 전사 출력 폴더(_transcripts/)의 백로그 색인.

  rec2md index <폴더> [--media-dir <녹음 폴더>]       # index.json 만들기·갱신 + index.md
  rec2md index set <폴더> <stem|원본> [--date 2099-01-15 --precision day --confidence high
                   --evidence "…" --topic "…" --attendees 내부회의 --summary "…" --name "…"]
  rec2md index approve <폴더> <stem|원본>... | --all-high
  rec2md index rename <폴더> [--apply] [--copy-media <폴더>] [--media-dir <녹음 폴더>]
  rec2md index rename <폴더> --undo [--last N]

- 기계적 칸(길이·말소리 길이·회의 구간·화자 수·날짜 단서 후보)은 CLI가, 날짜 추정·주제·요약은 에이전트가 채운다(SKILL.md).
- 다시 돌려도 에이전트가 채운 칸과 승인은 남는다.
- rename은 기본이 계획 출력(dry-run). 승인된 항목의 전사 출력 이름만 바꾼다. 녹음 원본에는 쓰지 않는다.
  --copy-media를 주면 원본의 사본을 새 이름으로 그 폴더에 만든다(원본과 같은 폴더면 거부).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import index as ix


def _dir(p: str) -> Path:
    d = Path(p)
    if not d.is_dir():
        raise SystemExit(f"폴더 없음: {d}")
    return d


def cmd_build(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md index", description="전사 출력 폴더의 백로그 색인 만들기·갱신")
    p.add_argument("dir", help="전사 출력 폴더 (batch의 _transcripts)")
    p.add_argument("--media-dir", action="append", type=Path, default=[],
                   help="원본 녹음 폴더(지문용, 상태 파일의 경로를 못 찾을 때). 여러 번 줄 수 있음")
    a = p.parse_args(argv)
    out = _dir(a.dir)
    idx = ix.build(out, a.media_dir)
    md = ix.write_md(out, idx)
    items = idx["items"]
    todo = sum(1 for i in items if not (i.get("agent") or {}).get("topic"))
    flags = {f: sum(1 for i in items if i.get(f)) for f in ("stale", "missing", "rec_dup")}
    print(f"색인 {len(items)}건 (에이전트 판단 필요 {todo}건) → {out / ix.INDEX_JSON}, {md}")
    for f, n in flags.items():
        if n:
            print(f"  {f}: {n}건")


def cmd_set(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md index set", description="항목의 에이전트 판단 칸 채우기")
    p.add_argument("dir")
    p.add_argument("key", help="stem 또는 원본 파일명")
    p.add_argument("--date", help="YYYY-MM-DD, YYYY-MM, YYYY")
    p.add_argument("--precision", choices=ix.PRECISIONS)
    p.add_argument("--confidence", choices=ix.CONFIDENCES)
    p.add_argument("--evidence", action="append", help="날짜 근거 한 줄(여러 번). 발화 원문은 쓰지 말고 종류·시각만")
    p.add_argument("--topic")
    p.add_argument("--attendees", choices=ix.ATTENDEE_TYPES)
    p.add_argument("--summary")
    p.add_argument("--name", help="제안 이름을 직접 지정(기본은 날짜·주제·REC 번호로 만듦)")
    a = p.parse_args(argv)
    out = _dir(a.dir)
    idx = ix.load_index(out)
    item = ix.find_item(idx, a.key)
    ix.set_agent(item, date=a.date, date_precision=a.precision, date_confidence=a.confidence,
                 date_evidence=a.evidence, topic=a.topic, attendee_type=a.attendees, summary=a.summary,
                 proposed_name=a.name)
    item["approved"] = False  # 판단이 바뀌면 승인을 다시 받는다
    ix.save_json(out / ix.INDEX_JSON, idx)
    ix.write_md(out, idx)
    print(f"{item['stem']}: 제안 이름 {ix.proposed(item)}")


def cmd_approve(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md index approve", description="이름 바꾸기 승인 표시(사용자 확인 후)")
    p.add_argument("dir")
    p.add_argument("keys", nargs="*", help="stem 또는 원본 파일명")
    p.add_argument("--all-high", action="store_true", help="날짜 확신도 high이고 주제가 있는 항목 전부")
    p.add_argument("--revoke", action="store_true", help="승인 취소")
    a = p.parse_args(argv)
    out = _dir(a.dir)
    idx = ix.load_index(out)
    items = [ix.find_item(idx, k) for k in a.keys]
    if a.all_high:
        items += [i for i in idx["items"] if (i.get("agent") or {}).get("date_confidence") == "high"
                  and (i.get("agent") or {}).get("topic") and not i.get("missing")]
    if not items:
        raise SystemExit("승인할 항목을 지정할 것 (stem, 원본 파일명, 또는 --all-high)")
    for i in items:
        i["approved"] = not a.revoke
    ix.save_json(out / ix.INDEX_JSON, idx)
    ix.write_md(out, idx)
    print(f"{'승인 취소' if a.revoke else '승인'} {len({id(i) for i in items})}건")


def cmd_rename(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md index rename",
                                description="승인된 항목의 전사 출력 이름 바꾸기 (기본은 계획만 출력)")
    p.add_argument("dir")
    p.add_argument("--apply", action="store_true", help="실제로 바꿈")
    p.add_argument("--copy-media", type=Path, help="원본 미디어의 사본을 새 이름으로 만들 폴더(원본 폴더 불가)")
    p.add_argument("--media-dir", action="append", type=Path, default=[], help="원본 녹음 폴더")
    p.add_argument("--undo", action="store_true", help="이름 바꾸기 되돌리기")
    p.add_argument("--last", type=int, help="--undo: 마지막 N건만")
    a = p.parse_args(argv)
    out = _dir(a.dir)
    idx = ix.load_index(out)
    if a.undo:
        done = ix.undo_renames(out, idx, a.last)
        ix.write_md(out, idx)
        for e in done:
            print(f"되돌림: {e['new']} → {e['old']}" + (f" (사본은 남아 있음: {e['media_copy']})" if e.get("media_copy") else ""))
        print(f"되돌림 {len(done)}건")
        return
    plans = ix.plan_renames(out, idx)
    if not plans:
        raise SystemExit("승인된 항목 없음 — 사용자 확인 뒤 `rec2md index approve <폴더> <stem>`")
    if a.apply:
        ix.apply_renames(out, idx, plans, a.copy_media, a.media_dir)
        ix.write_md(out, idx)
    for pl in plans:
        mark = "건너뜀" if pl["skip"] else ("바꿈" if a.apply else "바꿀 예정")
        why = f" — {pl['skip']}" if pl["skip"] else f" (파일 {len(pl['files'])}개)"
        print(f"[{mark}] {pl['old']} → {pl['new']}{why}")
    ok = sum(1 for pl in plans if not pl["skip"])
    print(f"{'바꿈' if a.apply else '계획'} {ok}건, 건너뜀 {len(plans) - ok}건"
          + ("" if a.apply else " — 실행하려면 --apply"))
    print("참고: 녹음기 안에서 직접 이름을 바꾸면 녹음기가 그 번호를 다시 쓴다(같은 RECnnnnn이 다른 녹음이 됨). "
          "이 명령은 녹음 원본을 건드리지 않는다.")


SUB = {"set": cmd_set, "approve": cmd_approve, "rename": cmd_rename}


def main(argv: list[str]) -> None:
    if argv and argv[0] in SUB:
        return SUB[argv[0]](argv[1:])
    return cmd_build(argv)
