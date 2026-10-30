"""용어 판정 사례 만들기: 엔진·클로바가 엇갈린 용어 구간 → <이름>.cases.json (rec2md review --queue용)

    uv run python scripts/term_cases.py --audio rec.mp3 --clova clova.txt \
        base=a.segments.json prompt=b.segments.json [--series <이름>] [--items items.tsv] \
        [--max 30] --out rec.cases.json

사례는 두 종류다.
- 지정 항목(--items): `항목<TAB>클로바 본문에서 찾을 표기[<TAB>분류]`. 사람이 들어 봐야 아는 것(인명 등)
- 엇갈림(자동): 한 시스템에는 용어집 표기가 있는데 같은 시각 다른 시스템에는 없는 구간
  (프롬프트·보정 전후로 표기가 바뀐 곳)

선택지는 각 시스템이 그 자리에 적은 표기와 클로바 표기다(같은 표기는 하나로 합친다, 최대 4개).
"모르겠음"은 화면이 붙인다. 답 파일은 eval_terms.py --cases/--answers로 읽는다.
녹취 기반 사례·답은 개인 데이터라 레포 밖(local/)에 둔다.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from rapidfuzz import fuzz

from rec2md import clova_txt
from rec2md.corrections import _spans
from rec2md.segments import Transcript
from rec2md.series import load_series

PAD = 2.0  # 시스템 간 구간을 맞출 때 허용하는 시각 차(초)
MAX_OPTIONS = 4


def norm(s: str) -> str:
    return re.sub(r"[^\w]", "", s)


def window(t: Transcript, a: float, b: float) -> str:
    return " ".join(s.text for s in t.segments if s.end > a - PAD and s.start < b + PAD)


def aligned(text: str, form: str) -> str | None:
    """text의 어절 묶음(1〜3어절, 조사 뗌) 중 form과 가장 비슷한 것. 너무 다르면 None.

    글자 단위로 맞추면 앞뒤 어절의 한 글자가 붙어 나오므로("때김한") 어절 경계에서 자른다.
    """
    best, score = None, 0.0
    for sp in _spans(text):
        r = fuzz.ratio(norm(form), sp)
        if r > score:
            best, score = sp, r
    return best if score >= 50 else None


def options(labels: list[str | None], form: str) -> list[dict]:
    # 한두 글자만 걸린 조각("수", "센터")은 선택지로 쓸모가 없다
    least = max(2, len(norm(form)) // 2)
    uniq = [x for x in dict.fromkeys(x for x in labels if x and len(norm(x)) >= least)][:MAX_OPTIONS]
    return [{"key": str(i), "label": x} for i, x in enumerate(uniq, 1)]


def item_cases(items: list[tuple[str, str, str]], systems: dict[str, Transcript], clova: Transcript,
               vocab: list[str]) -> list[dict]:
    first = next(iter(systems.values()))
    out = []
    for item, form, cat in items:
        seg = next((s for s in clova.segments if form in s.text), None)
        if seg is None:
            continue
        sent = next((x for x in re.split(r"(?<=[.?!])\s+", seg.text) if form in x), seg.text)
        near = [s for s in first.segments if s.end > seg.start - 5 and s.start < seg.end + 5]
        best = max(near, key=lambda s: fuzz.partial_ratio(norm(form), norm(s.text)) * 2
                   + fuzz.partial_ratio(sent, s.text), default=None)
        a, b = (best.start, best.end) if best else (seg.start, seg.start + 15)
        spans = [aligned(window(t, a, b), form) for t in systems.values()]
        cands = [v for v in vocab if fuzz.ratio(norm(v), norm(form)) >= 60]
        out.append({"kind": "term", "start": round(a, 2), "end": round(b, 2),
                    "question": f"'{form}'(클로바 표기)로 들리는 말의 정확한 표기는?",
                    "options": options(spans + [form] + cands, form), "allow_text": True,
                    "context": {"local": best.text if best else "", "clova": sent},
                    "meta": {"item": item, "category": cat, "clova_form": form}})
    return out


def disagreement_cases(systems: dict[str, Transcript], clova: Transcript, vocab: list[str]) -> list[dict]:
    names = list(systems)
    out, seen = [], set()
    for term in vocab:
        k = norm(term)
        if len(k) < 2:
            continue
        for name in names:
            for s in systems[name].segments:
                if k not in norm(s.text):
                    continue
                others = {n: window(systems[n], s.start, s.end) for n in names if n != name}
                if all(k in norm(x) for x in others.values()):
                    continue  # 모두 같은 표기면 판정할 게 없다
                key = (k, int(s.start // 10))
                if key in seen:
                    continue
                seen.add(key)
                cl = window(clova, s.start, s.end)
                labels = [term] + [aligned(x, term) for x in others.values()] + [aligned(cl, term)]
                out.append({"kind": "term", "start": round(s.start, 2), "end": round(s.end, 2),
                            "question": f"'{term}' 자리의 정확한 표기는?",
                            "options": options(labels, term), "allow_text": True,
                            "context": {"local": s.text, "clova": cl[:300]},
                            "meta": {"item": term, "category": "엇갈림", "found_in": name}})
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("systems", nargs="+", help="이름=segments.json (첫 번째가 구간 기준)")
    p.add_argument("--audio", required=True)
    p.add_argument("--clova", type=Path, required=True)
    p.add_argument("--series", help="엇갈림을 찾을 용어집")
    p.add_argument("--items", type=Path, help="지정 항목 TSV")
    p.add_argument("--max", type=int, default=30)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    systems = {n: Transcript.from_json(Path(x)) for n, _, x in (s.partition("=") for s in args.systems)}
    clova = clova_txt.parse(args.clova)
    vocab = load_series(args.series).vocabulary() if args.series else []
    items = []
    if args.items:
        for line in args.items.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                f = line.split("\t")
                items.append((f[0], f[1], f[2] if len(f) > 2 else "지정"))

    cases = item_cases(items, systems, clova, vocab)
    # 지정 항목을 먼저 채우고 남는 자리를 엇갈림으로. 엇갈림은 고르게 섞이도록 용어별로 돌아가며 뽑는다
    auto = disagreement_cases(systems, clova, vocab)
    by_term: dict[str, list[dict]] = {}
    for c in auto:
        by_term.setdefault(c["meta"]["item"], []).append(c)
    while len(cases) < args.max and any(by_term.values()):
        for term in list(by_term):
            if by_term[term] and len(cases) < args.max:
                cases.append(by_term[term].pop(0))
    cases = [c for c in cases if len(c["options"]) >= 1]
    for i, c in enumerate(cases, 1):
        c["id"] = f"B-{i:03d}"
        c["from"] = "worker-2"
    cases = [{"id": c.pop("id"), **c} for c in cases]
    data = {"version": 1, "audio": args.audio, "segments": str(Path(args.systems[0].partition("=")[2])),
            "cases": cases}
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{args.out}: 사례 {len(cases)}건 (지정 {sum(c['meta']['category'] != '엇갈림' for c in cases)}, "
          f"엇갈림 {sum(c['meta']['category'] == '엇갈림' for c in cases)})")


if __name__ == "__main__":
    main()
