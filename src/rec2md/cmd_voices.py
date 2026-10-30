"""rec2md voices enroll|list|rename|forget|match — 화자 목소리 DB 관리."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import voices
from .render import render_raw
from .segments import Transcript


def _mapping(text: str, allow_same: bool = False) -> dict[str, str]:
    out = {}
    for pair in text.split(","):
        label, sep, name = pair.partition("=")
        if not sep or not label.strip() or not name.strip():
            raise SystemExit(f"--map 형식: \"화자3=홍길동,화자5=김철수\" (받은 값: {pair!r})")
        out[label.strip()] = name.strip()
    if not allow_same and len(set(out.values())) < len(out):
        raise SystemExit("한 녹취에서 두 화자를 같은 이름에 등록할 수 없음(pyannote가 한 사람을 쪼갰다면 긴 쪽만 등록)")
    return out


def _longest_per_name(names: dict[str, str], side: dict) -> dict[str, str]:
    """검토 화면에서 쪼개진 두 화자에 같은 이름을 줬으면 발화가 긴 쪽만 등록한다(한 녹취 한 사람 한 임베딩)."""
    best: dict[str, str] = {}
    for lb, name in names.items():
        if lb in side and (name not in best or side[lb][1] > side[best[name]][1]):
            best[name] = lb
    return {lb: name for name, lb in best.items()}


def _save_and_show(t: Transcript, seg_path: Path) -> None:
    """segments.json과 (있으면) raw.md를 다시 쓰고 화자 이름 표를 찍는다."""
    t.to_json(seg_path)
    stem = seg_path.name.removesuffix(".segments.json")
    raw = seg_path.with_name(f"{stem}.raw.md")
    if raw.is_file():
        raw.write_text(render_raw(t, stem), encoding="utf-8")
    names = t.meta.get("speaker_names") or {}
    for label in sorted(names, key=lambda x: int(x[2:]) if x[2:].isdigit() else 0):
        d = names[label]
        note = (" ?" if d.get("tentative") else "") + (f" (같은 사람 후보: {d['same_as']})" if "same_as" in d else "")
        score = "사용자 지정" if d.get("source") == "user" else f"{d['score']:.3f}"
        print(f"{label}\t{d['name']}\t{score}{note}")
    if not names:
        print("이름이 붙은 화자 없음")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="rec2md voices", description="화자 목소리 DB (생체정보: 로컬에만 저장)")
    p.add_argument("--db", default=voices.DB_PATH, help=f"DB 경로 (기본: {voices.DB_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll", help="확정한 녹취의 화자를 실명으로 등록")
    e.add_argument("segments", type=Path, help="<이름>.segments.json (옆에 .voices.npz가 있어야 함)")
    how = e.add_mutually_exclusive_group(required=True)
    how.add_argument("--map", help='"화자3=홍길동,화자5=김철수"')
    how.add_argument("--from-names", action="store_true",
                     help='meta.speaker_names 중 사용자가 지정한 이름(source "user")을 등록. 짧은 화자는 건너뜀')
    e.add_argument("--min-sec", type=float, default=voices.MIN_ENROLL_SEC, help="이보다 짧은 화자는 거절")
    e.add_argument("--force", action="store_true",
                   help="이미 등록된 목소리와 닮지 않아도 등록 (원거리 녹음 등 확실할 때만. 방·마이크 특성을 닮아 오태깅 위험)")

    sub.add_parser("list", help="등록된 사람")
    r = sub.add_parser("rename", help="이름 바꾸기")
    r.add_argument("old")
    r.add_argument("new")
    f = sub.add_parser("forget", help="그 사람의 임베딩을 전부 지움")
    f.add_argument("name")

    m = sub.add_parser("match", help="이미 전사한 녹취에 다시 이름 붙이기 (segments.json·raw.md 갱신)")
    m.add_argument("segments", type=Path)
    m.add_argument("--threshold", type=float, default=voices.THRESHOLD)

    n = sub.add_parser("name", help="사용자가 확인한 화자 이름을 녹취에 기록 (source \"user\", DB는 안 건드림)")
    n.add_argument("segments", type=Path)
    n.add_argument("--map", default="", help='"화자3=홍길동,화자5=김철수" (쪼개진 화자는 같은 이름 가능)')
    n.add_argument("--unset", default="", help='이름을 뗄 화자: "화자3,화자5"')
    args = p.parse_args(argv)

    if args.cmd == "match":
        t = Transcript.from_json(args.segments)
        if not Path(args.db).is_file():
            raise SystemExit(f"목소리 DB 없음: {args.db} (먼저 enroll)")
        voices.annotate(t, voices.sidecar_path(args.segments), args.db, args.threshold)
        _save_and_show(t, args.segments)
        return
    if args.cmd == "name":
        t = Transcript.from_json(args.segments)
        mapping = _mapping(args.map, allow_same=True) if args.map else {}
        unset = [x.strip() for x in args.unset.split(",") if x.strip()]
        known = {s.speaker for s in t.segments if s.speaker}
        bad = [lb for lb in [*mapping, *unset] if lb not in known]
        if bad:
            raise SystemExit(f"이 녹취에 없는 화자: {', '.join(bad)} (있는 화자: {', '.join(sorted(known))})")
        if not mapping and not unset:
            raise SystemExit("--map 또는 --unset을 줘야 함")
        names = dict(t.meta.get("speaker_names") or {})
        for lb in unset:
            names.pop(lb, None)
        names.update({lb: {"name": nm, "source": "user"} for lb, nm in mapping.items()})
        t.meta["speaker_names"] = names
        _save_and_show(t, args.segments)
        return

    with voices.VoiceDB.open(args.db) as db:
        if args.cmd == "enroll":
            t = Transcript.from_json(args.segments)
            side = voices.load_sidecar(voices.sidecar_path(args.segments))
            if args.map:
                mapping = _mapping(args.map)
            else:
                mapping = _longest_per_name(voices.user_names(t), side)
                if not mapping:
                    raise SystemExit("사용자가 지정한 화자 이름(meta.speaker_names, source \"user\")이 없음")
            segs = voices.load_segments(voices.sidecar_path(args.segments))
            for msg in voices.enroll(db, side, mapping, t.source, args.min_sec, skip_short=args.from_names,
                                     segments=segs, force=args.force):
                print(msg)
        elif args.cmd == "list":
            rows = db.people()
            for name, n, sec in rows:
                print(f"{name}\t임베딩 {n}개\t{sec / 60:.1f}분")
            if not rows:
                print("등록된 사람 없음")
        elif args.cmd == "rename":
            db.rename(args.old, args.new)
            print(f"{args.old} → {args.new}")
        elif args.cmd == "forget":
            print(f"{args.name}: 임베딩 {db.forget(args.name)}개 삭제")
