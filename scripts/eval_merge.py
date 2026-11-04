"""과분할 합치기 실험: 작은 군집을 더 큰 군집에 임베딩 코사인으로 합칠 때 맞게/잘못 합치는 수를 규칙별로.

    uv run --extra diarize python scripts/eval_merge.py [--cache local/voices-eval] [--clova local/clova]

화자분리를 다시 돌리지 않는다. `scripts/eval_voices.py`가 남긴 녹취별 캐시(<stem>.diar.npz, 화자 수 힌트 없이 돌린 것)와
같은 stem의 클로바 txt만 쓴다. 각 군집의 정답은 시간이 가장 많이 겹치는 클로바 라벨이다(기본 라벨 "Attendees N"도
서로 다른 사람으로 본다). 두 군집의 정답 라벨이 같으면 "같은 사람"이다. 클로바가 한 사람을 둘로 나눴으면 맞게 합친 것도
잘못으로 세므로 보수적이다.

점수:
- cen: 두 군집 중심의 코사인
- seg2cen: 작은 군집의 구간 임베딩과 큰 군집 중심의 코사인 평균
- knn: 작은 군집의 구간마다 큰 군집 구간 중 가장 닮은 3개의 코사인 평균, 그것의 평균

규칙: 작은 군집마다 자기보다 발화가 긴 군집 중 점수 1위를 고르고, 1위 ≥ 임계값이고 1위 − 2위 ≥ 차이면 합친다.
표준출력은 집계 수치만(이름·파일명 없음).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from compare import FRAME, frames  # noqa: E402

from rec2md import clova_txt, voices  # noqa: E402
from rec2md.segments import Segment, Transcript  # noqa: E402

KEYS = ("cen", "seg2cen", "knn")
THRESHOLDS = (0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75)


def pairs_of(npz: Path, clova: Transcript) -> tuple[list[dict], int]:
    """(작은 군집 → 더 큰 군집 쌍 목록, 군집 수)"""
    z = np.load(npz)
    turns = [(float(s), float(e), str(x)) for s, e, x in zip(z["start"], z["end"], z["spk"])]
    cen = {str(x): voices._unit(v) for x, v in zip(z["labels"], z["vecs"])}
    segs: dict[str, list] = defaultdict(list)
    for x, v in zip(z["seg_spk"], z["seg_vecs"]):
        segs[str(x)].append(voices._unit(v))
    n = int(max(clova.duration, max(e for _, e, _ in turns)) / FRAME) + 1
    ours = Transcript(source="", duration=n * FRAME, engine="", language="ko",
                      segments=[Segment(s, e, "", x) for s, e, x in turns])
    overlap = Counter((a, b) for a, b in zip(frames(ours, n), frames(clova, n)) if a and b)
    gt = {x: max(((c, b) for (a, b), c in overlap.items() if a == x), default=(0, None))[1] for x in cen}
    secs: Counter = Counter()
    for s, e, x in turns:
        secs[x] += e - s
    order = sorted(cen, key=lambda x: -secs[x])
    out = []
    for i, x in enumerate(order):
        S = np.stack(segs.get(x) or [cen[x]])
        for y in order[:i]:
            T = np.stack(segs.get(y) or [cen[y]])
            out.append({"small": x, "sec": secs[x], "same": gt[x] is not None and gt[x] == gt[y],
                        "cen": float(cen[x] @ cen[y]), "seg2cen": float((S @ cen[y]).mean()),
                        "knn": float(np.sort(S @ T.T, axis=1)[:, -3:].mean())})
    return out, len(cen)


def apply_rule(by_small: dict, key: str, th: float, gap: float, max_sec: float) -> tuple[int, int]:
    ok = wrong = 0
    for cand in by_small.values():
        if cand[0]["sec"] >= max_sec:
            continue
        cand = sorted(cand, key=lambda p: -p[key])
        b1, b2 = cand[0][key], (cand[1][key] if len(cand) > 1 else -1.0)
        if b1 >= th and b1 - b2 >= gap:
            ok += cand[0]["same"]
            wrong += not cand[0]["same"]
    return ok, wrong


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--cache", type=Path, default=Path("local/voices-eval"))
    p.add_argument("--clova", type=Path, default=Path("local/clova"))
    args = p.parse_args()

    by_small: dict = {}
    stats = []
    for k, npz in enumerate(sorted(args.cache.glob("*.diar.npz"))):
        txt = args.clova / (npz.name.removesuffix(".diar.npz") + ".txt")
        if not txt.is_file():
            continue
        clova = clova_txt.parse(txt)
        pairs, n = pairs_of(npz, clova)
        stats.append((round(clova.duration / 60), n, len({s.speaker for s in clova.segments})))
        for q in pairs:
            by_small.setdefault((k, q["small"]), []).append(q)

    allp = [q for c in by_small.values() for q in c]
    same = [q for q in allp if q["same"]]
    print(f"녹취 {len(stats)}건 (분, 군집, 클로바 화자): {stats}")
    print(f"쌍 {len(allp)}개 중 같은 사람 {len(same)}개, "
          f"같은 사람이 더 큰 군집에 있는 작은 군집 {sum(any(q['same'] for q in c) for c in by_small.values())}개\n")
    for key in KEYS:
        diff = sorted(q[key] for q in allp if not q["same"])
        print(f"{key}: 다른 사람 최고 {diff[-1]:.2f}, 같은 사람 "
              + " ".join(f"{q[key]:.2f}" for q in sorted(same, key=lambda q: -q[key])))
    for key in KEYS:
        print(f"\n| {key} 차이 \\ 임계값 | " + " | ".join(f"{t:.2f}" for t in THRESHOLDS) + " |")
        print("|---|" + "---|" * len(THRESHOLDS))
        for gap in (0.0, 0.05, 0.1):
            cells = [apply_rule(by_small, key, t, gap, float("inf")) for t in THRESHOLDS]
            print(f"| {gap:.2f} | " + " | ".join(f"{o}/{w}" for o, w in cells) + " |")
    print("\n칸은 맞게 합침/잘못 합침")


if __name__ == "__main__":
    main()
