"""두 전사 결과(예: local vs 클로바노트) 비교.

    uv run python scripts/compare.py <a.segments.json> <clova.txt|b.segments.json> [--side-by-side out.md]

정답(사람 전사)이 없으므로 지표는 "두 엔진이 얼마나 다르게 적었나"다. 어느 쪽이 맞는지는
나란히 보기(--side-by-side)로 사람이 판단한다.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from rapidfuzz.distance import Levenshtein

from rec2md import clova_txt
from rec2md.render import fmt_ts
from rec2md.segments import Transcript

FRAME = 0.5


def load(p: Path) -> Transcript:
    return clova_txt.parse(p) if p.suffix == ".txt" else Transcript.from_json(p)


def norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text)


def cer(ref: str, hyp: str) -> float:
    return Levenshtein.distance(ref, hyp) / max(len(ref), 1)


def frames(t: Transcript, n: int) -> list[str | None]:
    out: list[str | None] = [None] * n
    for s in t.segments:
        for i in range(int(s.start / FRAME), min(int(s.end / FRAME), n)):
            out[i] = s.speaker
    return out


def speaker_agreement(a: Transcript, b: Transcript) -> tuple[float, dict[str, str]]:
    """b의 화자에 a의 화자를 겹침이 큰 순서로 1:1 대응시킨 뒤 프레임 일치율."""
    n = int(max(a.duration, b.duration) / FRAME) + 1
    fa, fb = frames(a, n), frames(b, n)
    pairs = Counter((x, y) for x, y in zip(fa, fb) if x and y)
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for (x, y), _ in pairs.most_common():
        if x not in mapping and y not in used:
            mapping[x] = y
            used.add(y)
    both = sum(pairs.values())
    agree = sum(c for (x, y), c in pairs.items() if mapping.get(x) == y)
    return agree / max(both, 1), mapping


def side_by_side(a: Transcript, b: Transcript, mapping: dict[str, str], la: str, lb: str) -> str:
    out = [f"| 시각 | {lb} | {la} |", "|---|---|---|"]
    for s in b.segments:
        hits = [x for x in a.segments if x.end > s.start and x.start < s.end]
        mine = " ".join(f"({mapping.get(x.speaker, x.speaker)}) {x.text}" for x in hits)
        cell = lambda t: t.replace("|", "\\|")
        out.append(f"| {fmt_ts(s.start)} | ({s.speaker}) {cell(s.text)} | {cell(mine)} |")
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    p.add_argument("--side-by-side", type=Path)
    args = p.parse_args()
    a, b = load(args.a), load(args.b)
    ta, tb = norm(" ".join(s.text for s in a.segments)), norm(" ".join(s.text for s in b.segments))
    agree, mapping = speaker_agreement(a, b)
    spk_a = {s.speaker for s in a.segments if s.speaker}
    spk_b = {s.speaker for s in b.segments if s.speaker}
    print(f"길이 {fmt_ts(b.duration)} | 글자수 a={len(ta)} b={len(tb)} | CER(b 기준)={cer(tb, ta):.1%}")
    print(f"화자수 a={len(spk_a)} b={len(spk_b)} | 화자 프레임 일치율={agree:.1%} | 대응 {mapping}")
    if args.side_by_side:
        args.side_by_side.write_text(side_by_side(a, b, mapping, a.engine, b.engine), encoding="utf-8")


if __name__ == "__main__":
    main()
