"""고유명사 적중률 평가: 용어집 프롬프트·보정 후보가 고유명사 표기를 얼마나 맞추는가.

    uv run python scripts/eval_terms.py --gold gold.txt --ref clova.txt \
        base=a.segments.json prompt=b.segments.json clova=clova.txt [--series <이름>]

- gold.txt: 한 줄에 `분류<TAB>표기<TAB>출처`. 분류·출처는 생략할 수 있다. `#`는 주석
  출처: directory(원내 명부) · clova-manual(사용자가 클로바에서 직접 붙인 화자명·표기) · doc(문서) ·
  user(사용자 확인) · inferred(에이전트 추정). **헤드라인은 inferred를 뺀 gold로만** 낸다
- 사람 전사가 없으므로 용어별 "실제 등장 횟수"는 모든 시스템 중 가장 많이 맞게 적은 횟수로 어림한다.
  어느 시스템도 한 번도 못 맞춘 용어는 분모에서 빠진다(그 용어는 따로 목록으로 보여준다).
- 정답 횟수는 +fix를 뺀 시스템들로만 어림한다.
- --series를 주면 segments.json마다 보정 후보의 첫 제안을 기계적으로 적용한 `<이름>+fix`를 더한다.
  에이전트 교정의 상한에 가까운 값이다(근거 확인 없이 다 받아들였을 때).
- CER은 --ref(보통 클로바) 기준. 클로바도 틀리므로 "클로바와 얼마나 다른가"로 읽는다. 악화 여부만 본다.

사람 판정(gold v1): --cases <이름>.cases.json --answers <이름>.answers.json을 주면 사례마다 답(출처 user)이
각 시스템의 그 구간(앞뒤 2초) 본문에 들어 있는지를 센다. 횟수 어림이 필요 없는 직접 비교라 이쪽을 우선한다.
답한 표기는 gold 용어 목록에도 출처 user로 더해진다.

녹취·정답 목록은 개인정보라 레포 밖(local/)에 둔다.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from rapidfuzz.distance import Levenshtein

from rec2md import clova_txt
from rec2md.corrections import find_candidates
from rec2md.segments import Transcript
from rec2md.series import load_series


def norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text)


def load(p: Path) -> Transcript:
    return clova_txt.parse(p) if p.suffix == ".txt" else Transcript.from_json(p)


def text_of(t: Transcript) -> str:
    return norm(" ".join(s.text for s in t.segments))


def apply_fixes(t: Transcript, series: str) -> str:
    """후보 중 용어집 근거(오인식 매핑·자모거리)가 있는 것만 첫 제안으로 바꾼 본문."""
    cands = find_candidates(t, load_series(series))
    by_ts: dict[float, list[dict]] = defaultdict(list)
    for c in cands:
        if c["suggest"] and any("자모거리" in r or "매핑" in r for r in c["reasons"]):
            by_ts[c["ts"]].append(c)
    out = []
    for s in t.segments:
        body = norm(s.text)
        for c in by_ts.get(round(s.start, 2), []):
            body = body.replace(norm(c["text"]), norm(c["suggest"][0]))
        out.append(body)
    return "".join(out)


SOURCES = ["directory", "clova-manual", "doc", "user", "inferred"]


def read_gold(p: Path) -> list[tuple[str, str, str]]:
    """[(분류, 표기, 출처)]. 출처가 없으면 inferred로 본다(확인된 것만 헤드라인에 넣기 위해)."""
    gold = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        f = [x.strip() for x in line.split("\t")]
        if len(f) == 1:
            f = ["전체", f[0], "inferred"]
        elif len(f) == 2:
            f.append("inferred")
        cat, term, src = f[:3]
        if src not in SOURCES:
            raise SystemExit(f"알 수 없는 출처 '{src}': {line}")
        gold.append((cat or "전체", term, src))
    return gold


PAD = 2.0


def load_answers(cases_path: Path, answers_path: Path) -> list[dict]:
    """답한 사례만 [{case, answer}]. 모르겠음·빈 답은 뺀다."""
    cases = {c["id"]: c for c in json.loads(cases_path.read_text(encoding="utf-8"))["cases"]}
    out = []
    for a in json.loads(answers_path.read_text(encoding="utf-8")):
        c = cases.get(a["id"])
        if c is None or a.get("unsure"):
            continue
        label = (a.get("text") or "").strip() or next((o["label"] for o in c["options"] if o["key"] == a.get("key")), "")
        if label:
            out.append({"case": c, "answer": label})
    return out


def case_table(answered: list[dict], systems: dict[str, Transcript]) -> None:
    groups = sorted({x["case"].get("meta", {}).get("category", "전체") for x in answered})
    print(f"사람 판정 gold v1 (출처 user), 답한 사례 n={len(answered)}\n")
    print("| 시스템 | 전체 | " + " | ".join(groups) + " |")
    print("|" + "---|" * (len(groups) + 2))
    for name, t in systems.items():
        def hit(x):
            c = x["case"]
            body = norm(" ".join(s.text for s in t.segments if s.end > c["start"] - PAD and s.start < c["end"] + PAD))
            return norm(x["answer"]) in body
        def rate(sel):
            return f"{sum(map(hit, sel)) / len(sel):.0%} ({sum(map(hit, sel))}/{len(sel)})" if sel else "-"
        cells = [rate(answered)] + [rate([x for x in answered if x["case"].get("meta", {}).get("category", "전체") == g])
                                    for g in groups]
        print(f"| {name} | " + " | ".join(cells) + " |")
    print()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("systems", nargs="+", help="이름=경로 (segments.json 또는 클로바 txt)")
    p.add_argument("--gold", type=Path, help="용어 목록 (없으면 --answers만으로)")
    p.add_argument("--ref", type=Path, help="CER 기준 (클로바 txt)")
    p.add_argument("--series", help="보정 후보 적용본(+fix)도 평가")
    p.add_argument("--detail", action="store_true", help="용어별 횟수 표")
    p.add_argument("--cases", type=Path, help="판정 사례 (term_cases.py)")
    p.add_argument("--answers", type=Path, help="판정 답 (rec2md review --queue)")
    args = p.parse_args()

    texts: dict[str, str] = {}
    loaded: dict[str, Transcript] = {}
    for spec in args.systems:
        name, _, path = spec.partition("=")
        t = loaded[name] = load(Path(path))
        texts[name] = text_of(t)
        if args.series and path.endswith(".segments.json"):
            texts[f"{name}+fix"] = apply_fixes(t, args.series)

    gold = read_gold(args.gold) if args.gold else []
    if args.cases and args.answers:
        answered = load_answers(args.cases, args.answers)
        case_table(answered, loaded)
        known = {norm(g[1]) for g in gold}
        for x in answered:
            if norm(x["answer"]) not in known:
                known.add(norm(x["answer"]))
                gold.append((x["case"].get("meta", {}).get("category", "전체"), x["answer"], "user"))
    if not gold:
        return
    counts = {n: [txt.count(norm(term)) for _, term, _ in gold] for n, txt in texts.items()}
    # +fix는 정답 횟수 어림에 넣지 않는다(기계 치환이 없던 용어를 만들어 낼 수 있으므로). 넘친 만큼은 따로 보인다
    base = [c for n, c in counts.items() if not n.endswith("+fix")]
    ref = [max(c[i] for c in base) for i in range(len(gold))]

    confirmed = [i for i, g in enumerate(gold) if g[2] != "inferred"]
    by_src = {src: [i for i, g in enumerate(gold) if g[2] == src] for src in SOURCES}
    by_src = {k: v for k, v in by_src.items() if v}
    cats = sorted({g[0] for g in gold})
    print(f"gold = {'+'.join(k for k in by_src if k != 'inferred') or '(없음)'}, n={len(confirmed)}"
          f" (inferred {len(by_src.get('inferred', []))}개는 헤드라인 제외)\n")
    cols = ["헤드라인(출처 확보)"] + [f"출처:{k}" for k in by_src] + [f"분류:{c}" for c in cats]
    head = "| 시스템 | " + " | ".join(cols) + (" | CER(ref) |" if args.ref else " |")
    print(head)
    print("|" + "---|" * (head.count("|") - 1))
    ref_text = text_of(load(args.ref)) if args.ref else None
    for n, c in counts.items():
        def rate(sel):
            den = sum(ref[i] for i in sel)
            return f"{sum(min(c[i], ref[i]) for i in sel) / den:.0%} ({sum(min(c[i], ref[i]) for i in sel)}/{den})" if den else "-"
        cells = ([rate(confirmed)] + [rate(v) for v in by_src.values()]
                 + [rate([i for i, g in enumerate(gold) if g[0] == cat]) for cat in cats])
        if ref_text is not None:
            cells.append(f"{Levenshtein.distance(ref_text, texts[n]) / max(len(ref_text), 1):.1%}")
        print(f"| {n} | " + " | ".join(cells) + " |")

    for n, c in counts.items():
        over = [(gold[i][1], c[i] - ref[i]) for i in range(len(gold)) if c[i] > ref[i]]
        if over:
            print(f"\n{n}: 어림 횟수보다 많이 나온 용어(과잉 교정이거나 다른 시스템이 다 놓친 것) "
                  + ", ".join(f"{t}+{d}" for t, d in over))
    missing = [term for (_, term, _), r in zip(gold, ref) if r == 0]
    if missing:
        print(f"\n어느 시스템도 못 맞춘 용어 {len(missing)}개(분모 제외): {', '.join(missing)}")
    if args.detail:
        names = list(counts)
        print("\n| 용어 | 출처 | " + " | ".join(names) + " |")
        print("|" + "---|" * (len(names) + 2))
        for i, (_, term, src) in enumerate(gold):
            print(f"| {term} | {src} | " + " | ".join(str(counts[n][i]) for n in names) + " |")


if __name__ == "__main__":
    main()
