"""목소리 DB 자동 태깅 평가: leave-one-out으로 오태깅률·미태깅률을 임계값별로.

    uv run --extra diarize python scripts/eval_voices.py <audio_dir> <clova_dir> [옵션]
        --pair 오디오=클로바.txt      파일 이름(stem)이 다른 쌍을 더할 때 (여러 번)
        --gold gold.json              화자별 출처·이름 보정 (로컬 전용)
        --enroll robust|centroid      등록 벡터: 중심에서 먼 구간을 뺀 평균 / pyannote 클러스터 중심 (기본: 둘 다)
        --cases-at 0.6                이 임계값의 오태깅 사례를 시각과 함께 <cache>/mistag-cases.md로
        --queue DIR                   사람 판정용 <stem>.cases.json (rec2md review --queue) 생성
        --answers DIR                 판정 결과 <stem>.answers.json을 gold v1(출처 user)로 반영

정답과 출처
- 클로바 txt의 화자명을 scripts/compare.py의 그리디 매핑으로 우리 클러스터에 대응시킨다. 클로바 이름은 "문서 전체"
  범위로 붙은 것이라 클로바가 두 사람을 한 화자로 묶었으면 틀린 구간까지 퍼져 있다. 그래서 화자 단위로 순도를 본다:
  우리 클러스터 시간의 70% 이상이 그 클로바 라벨이고, 그 클로바 라벨 시간의 70% 이상이 우리 클러스터일 때만 정답.
  못 미치면 "불순"으로 따로 센다(채점·등록에서 뺌).
- 출처: clova-manual(사용자가 클로바에서 직접 붙임), user(판정 화면에서 확인), inferred(에이전트 추정).
  헤드라인은 --sources(기본 clova-manual,user)만으로 낸다. 기본 라벨("Attendees 3" 등)은 정답이 아니다.
  --gold JSON: {"<오디오 stem>": {"default": "clova-manual",
                                   "names": {"<클로바 라벨>": {"name": "실명", "source": "user"} | "inferred"}}}
- 판정(--answers)이 있는 클러스터는 답 2개 이상에서 70% 이상 같은 이름이면 그 이름(user)으로 바꾸고,
  답이 갈리면 불순으로 뺀다.

화자분리만 돌린다(전사 불필요). 결과는 --cache에 녹취별 npz로 남겨 다시 쓴다. 생체정보이므로 local/ 아래에 둔다.
표준출력은 집계 수치만(이름 없음). 사례 파일·--verbose에는 이름이 들어가니 로컬에서만 볼 것.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from compare import FRAME, frames, speaker_agreement  # noqa: E402

from rec2md import clova_txt, voices  # noqa: E402
from rec2md.cli import ENV_FILE  # noqa: E402
from rec2md.render import fmt_ts  # noqa: E402
from rec2md.segments import Segment, Transcript  # noqa: E402

AUDIO = {".mp3", ".m4a", ".wav", ".mp4", ".mov"}
NOT_A_NAME = re.compile(r"^(Attendees|참석자|화자|Speaker)\s*\d+$|^\d", re.I)
SOURCES = ("clova-manual", "user", "inferred")
PURITY = 0.7
CASES_PER_REC = (20, 30)
CASE_SEC = (3.0, 8.0)
NONE_OF_THEM = "둘 다 아님(다른 사람)"


@dataclass
class Rec:
    rid: str
    audio: Path
    clova: Transcript
    turns: list
    embs: dict[str, np.ndarray]
    segs: dict[str, list]  # 라벨 → [(start, end, 벡터)]
    secs: dict[str, float]
    label_gold: dict[str, tuple[str, str]]  # 클로바 라벨 → (실명, 출처)
    truth: dict[str, tuple[str, str]] = field(default_factory=dict)  # 우리 라벨 → (실명, 출처)
    impure: dict[str, str] = field(default_factory=dict)  # 우리 라벨 → 사유
    clova_of: dict[str, str] = field(default_factory=dict)  # 우리 라벨 → 대응한 클로바 라벨(불순 포함)


def diarize_cached(audio: Path, cache: Path) -> tuple[list, dict[str, np.ndarray], list]:
    """(turns, {라벨: 중심}, [(start, end, 라벨, 구간 임베딩)])"""
    npz = cache / f"{audio.stem}.diar.npz"
    if npz.is_file():
        with np.load(npz) as z:
            turns = [(float(s), float(e), str(k)) for s, e, k in zip(z["start"], z["end"], z["spk"])]
            segs = [(float(s), float(e), str(k), v)
                    for s, e, k, v in zip(z["seg_start"], z["seg_end"], z["seg_spk"], z["seg_vecs"])]
            return turns, {str(k): v for k, v in zip(z["labels"], z["vecs"])}, segs
    import tempfile

    from rec2md.audio import normalize
    from rec2md.diarize import diarize_with_embeddings

    with tempfile.TemporaryDirectory() as td:
        turns, embs, segs = diarize_with_embeddings(normalize(audio, Path(td) / "a.wav"))
    np.savez(npz, start=[t[0] for t in turns], end=[t[1] for t in turns], spk=[t[2] for t in turns],
             labels=list(embs), vecs=np.stack(list(embs.values())),
             seg_start=[x[0] for x in segs], seg_end=[x[1] for x in segs], seg_spk=[x[2] for x in segs],
             seg_vecs=np.stack([x[3] for x in segs]))
    os.chmod(npz, 0o600)
    return turns, embs, segs


def _ours(turns: list, duration: float) -> Transcript:
    return Transcript(source="", duration=duration, engine="", language="ko",
                      segments=[Segment(s, e, "", spk) for s, e, spk in turns])


def map_clova(rec: Rec) -> None:
    """클로바 라벨을 우리 클러스터에 대응시키고 순도 기준으로 정답·불순을 나눈다."""
    ours = _ours(rec.turns, rec.clova.duration)
    _, mapping = speaker_agreement(ours, rec.clova)
    n = int(rec.clova.duration / FRAME) + 1
    pairs = Counter((a, b) for a, b in zip(frames(ours, n), frames(rec.clova, n)) if a and b)
    mine, theirs = Counter(), Counter()
    for (a, b), c in pairs.items():
        mine[a] += c
        theirs[b] += c
    for x, y in mapping.items():
        rec.clova_of[x] = y
        if y not in rec.label_gold:
            continue
        both = pairs[x, y]
        px, py = both / max(mine[x], 1), both / max(theirs[y], 1)
        if px >= PURITY and py >= PURITY:
            rec.truth[x] = rec.label_gold[y]
        else:
            rec.impure[x] = f"클러스터 중 {px:.0%}가 그 클로바 화자, 그 클로바 화자 중 {py:.0%}가 이 클러스터"


def apply_answers(rec: Rec, cases: list[dict], answers: list[dict]) -> int:
    """판정 결과로 클러스터 정답을 user 출처로 바꾼다. 바뀐 클러스터 수."""
    by_id = {a["id"]: a for a in answers}
    votes: dict[str, Counter] = {}
    for c in cases:
        a = by_id.get(c["id"])
        if not a or a.get("unsure") or not a.get("key"):
            continue
        label = next((o["label"] for o in c["options"] if o["key"] == a["key"]), None)
        votes.setdefault(c["meta"]["cluster"], Counter())[label] += 1
    for x, cnt in votes.items():
        name, top = cnt.most_common(1)[0]
        total = sum(cnt.values())
        rec.impure.pop(x, None)
        rec.truth.pop(x, None)
        if total >= 2 and top / total >= PURITY:
            if name != NONE_OF_THEM:
                rec.truth[x] = (name, "user")
        else:
            rec.impure[x] = f"판정이 갈림 {dict(cnt)}"
    return len(votes)


def build_people(recs: list[Rec], skip: str, use: set[str], min_sec: float, mode: str) -> dict[str, np.ndarray]:
    people: dict[str, list[np.ndarray]] = {}
    for r in recs:
        if r.rid == skip:
            continue
        for lb, (name, src) in r.truth.items():
            if src in use and r.secs.get(lb, 0) >= min_sec and lb in r.embs:
                vec = voices.robust_vector(r.embs[lb], r.segs.get(lb, []))[0] if mode == "robust" \
                    else voices._unit(r.embs[lb])
                people.setdefault(name, []).append(vec)
    return {k: np.stack(v) for k, v in people.items()}


def clusters_of(r: Rec, use: set[str]) -> dict[str, tuple[np.ndarray, float]]:
    """채점 대상 클러스터: 불순하거나 다른 출처 정답인 것은 뺀다."""
    return {lb: (v, r.secs.get(lb, 0.0)) for lb, v in r.embs.items()
            if lb not in r.impure and not (lb in r.truth and r.truth[lb][1] not in use)}


def evaluate(recs: list[Rec], use: set[str], min_sec: float, mode: str, th: float):
    """(집계, 사례 목록)"""
    agg = Counter()
    cases = []
    for r in recs:
        people = build_people(recs, r.rid, use, min_sec, mode)
        clusters = clusters_of(r, use)
        got = voices.match(people, clusters, threshold=th)
        for lb, (_, sec) in clusters.items():
            gt = r.truth.get(lb, (None, None))[0]
            enrolled = gt in people
            agg["known"] += enrolled
            if lb not in got:
                agg["miss"] += enrolled
                continue
            agg["tag"] += 1
            agg["tag_sec"] += sec
            if gt is None:
                agg["stray"] += 1  # 정답이 없는 클러스터(짧거나 대응 없음) — 맞는지 알 수 없음
                cases.append((r, lb, None, got[lb]))
            elif got[lb]["name"] == gt:
                agg["ok"] += 1
            else:
                agg["wrong"] += 1
                agg["wrong_sec"] += sec
                cases.append((r, lb, gt, got[lb]))
    return agg, cases


def print_table(recs: list[Rec], use: set[str], min_sec: float, mode: str) -> None:
    print(f"\n등록 방식: {mode}\n")
    print("| 임계값 | 태깅 | 맞음 | 오태깅 | 오태깅률(판정 가능 태깅 중) | 미태깅(등록된 사람) "
          "| 정답 없는 클러스터에 태깅 | 오태깅 발화시간 비율 |")
    print("|---|---|---|---|---|---|---|---|")
    for th in np.arange(0.30, 0.91, 0.05):
        a, _ = evaluate(recs, use, min_sec, mode, float(th))
        judged = a["ok"] + a["wrong"]
        print(f"| {th:.2f} | {a['tag']} | {a['ok']} | {a['wrong']} | {a['wrong'] / max(judged, 1):.1%} | "
              f"{a['miss']}/{a['known']} ({a['miss'] / max(a['known'], 1):.0%}) | {a['stray']} | "
              f"{a['wrong_sec'] / max(a['tag_sec'], 1e-9):.1%} |")


def write_mistag_cases(path: Path, cases: list, th: float) -> None:
    lines = [f"# 오태깅·정답없음 태깅 사례 (임계값 {th:.2f}) — 개인정보, 로컬 전용", "",
             "클로바 쪽 오류일 수도 있다. 시각을 들어 보고 판정할 것.", ""]
    for r, lb, gt, hit in cases:
        longest = sorted((t for t in r.turns if t[2] == lb), key=lambda t: t[0] - t[1])[:6]
        same = f", same_as {hit['same_as']}" if "same_as" in hit else ""
        lines += [f"## {r.audio.name} · {lb} ({r.secs.get(lb, 0) / 60:.1f}분)",
                  f"- 정답: {gt or '없음'} (클로바 대응 라벨: {r.clova_of.get(lb, '-')}) / 태깅: {hit['name']} "
                  f"(점수 {hit['score']:.3f}{same})",
                  "- 긴 구간: " + ", ".join(f"{fmt_ts(s)}〜{fmt_ts(e)}" for s, e, _ in sorted(longest)), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)


def _clova_at(clova: Transcript, s: float, e: float) -> tuple[str | None, str]:
    best, text, best_ov = None, "", 0.0
    for seg in clova.segments:
        ov = min(e, seg.end) - max(s, seg.start)
        if ov > best_ov:
            best, best_ov, text = seg.speaker, ov, seg.text
    return best, text


def _clip(s: float, e: float) -> tuple[float, float] | None:
    """화자분리 구간 가운데 3〜8초. 3초보다 짧으면 판정하기 어려워 뺀다."""
    if e - s < CASE_SEC[0]:
        return None
    mid, half = (s + e) / 2, min(e - s, CASE_SEC[1]) / 2
    return round(mid - half, 2), round(mid + half, 2)


def write_queue(out: Path, recs: list[Rec], use: set[str], min_sec: float, mode: str, th: float,
                seed: int = 0) -> None:
    """녹취마다 판정 사례 20〜30개: 엇갈림(우리 vs 클로바) → 불순 화자 → 중심에서 먼 구간 → 일치 표본(약 20%)."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    rng = random.Random(seed)
    next_id = 1
    for r in recs:
        people = build_people(recs, r.rid, use, min_sec, mode)
        got = voices.match(people, clusters_of(r, use), threshold=th)
        disagree, impure, outlier, agree = [], [], [], []
        # 구간 임베딩이 제 클러스터 중심에서 먼 구간: 다른 사람이 섞였을 수 있다
        far = {(round(s, 2), lb) for lb, xs in r.segs.items() if lb in r.embs
               for s, _, v in xs if float(voices._unit(v) @ voices._unit(r.embs[lb])) < voices.OUTLIER_COS}
        for s, e, lb in r.turns:
            clip = _clip(s, e)
            if clip is None:
                continue
            cl, text = _clova_at(r.clova, s, e)
            clova_name = r.label_gold[cl][0] if cl in r.label_gold else None
            ours = got.get(lb, {}).get("name") or r.truth.get(lb, (None,))[0]
            item = (clip, lb, clova_name, ours, text)
            if lb in r.impure:
                impure.append(item)
            elif clova_name and ours and clova_name != ours:
                disagree.append(item)
            elif (round(s, 2), lb) in far:
                outlier.append(item)
            elif clova_name and ours:
                agree.append(item)
        hi = CASES_PER_REC[1]
        n_agree = max(1, round(hi * 0.2))
        picked = [("disagree", x) for x in rng.sample(disagree, min(len(disagree), hi - n_agree))]
        room = hi - n_agree - len(picked)
        # 불순 화자는 클러스터마다 고루 뽑는다(한 화자에 몰리지 않게)
        by_cluster: dict[str, list] = {}
        for x in impure:
            by_cluster.setdefault(x[1], []).append(x)
        for xs in by_cluster.values():
            rng.shuffle(xs)
        while room > 0 and any(by_cluster.values()):
            for xs in by_cluster.values():
                if xs and room > 0:
                    picked.append(("impure", xs.pop()))
                    room -= 1
        picked += [("outlier", x) for x in rng.sample(outlier, min(len(outlier), max(room, 0)))]
        n_agree = max(n_agree, CASES_PER_REC[0] - len(picked))
        picked += [("agree", x) for x in rng.sample(agree, min(len(agree), n_agree))]
        picked.sort(key=lambda p: p[1][0][0])

        cases = []
        for why, ((s, e), lb, clova_name, ours, text) in picked:
            names = [ours, clova_name]
            if lb in r.impure and r.clova_of.get(lb) in r.label_gold:
                names.append(r.label_gold[r.clova_of[lb]][0])
            opts = [{"key": str(i + 1), "label": n} for i, n in enumerate(dict.fromkeys(x for x in names if x))]
            opts.append({"key": str(len(opts) + 1), "label": NONE_OF_THEM})
            cases.append({"id": f"A-{next_id:03d}", "kind": "speaker", "start": s, "end": e,
                          "question": "이 구간의 화자는?", "options": opts,
                          "context": {"clova": text}, "from": "worker-1",
                          "meta": {"cluster": lb, "why": why}})
            next_id += 1
        path = out / f"{r.audio.stem}.cases.json"
        path.write_text(json.dumps({"version": 1, "audio": str(r.audio.resolve()), "cases": cases},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)
        kinds = Counter(w for w, _ in picked)
        print(f"[{r.rid}] 판정 사례 {len(cases)}개 (엇갈림 {kinds['disagree']}, 불순 {kinds['impure']}, "
              f"먼 구간 {kinds['outlier']}, 일치 {kinds['agree']}) → {path}", file=sys.stderr)


def load_recs(args) -> list[Rec]:
    gold = json.loads(args.gold.read_text(encoding="utf-8")) if args.gold else {}
    pairs: list[tuple[Path, Path]] = []
    for txt in sorted(args.clova_dir.glob("*.txt")):
        audio = next((a for a in args.audio_dir.iterdir() if a.stem == txt.stem and a.suffix.lower() in AUDIO), None)
        if audio is not None:
            pairs.append((audio, txt))
    for pair in args.pair:
        audio, _, txt = pair.partition("=")
        pairs.append((Path(audio), Path(txt)))

    recs = []
    for audio, txt in pairs:
        clova = clova_txt.parse(txt)
        g = gold.get(audio.stem, {})
        label_gold = {}  # 클로바 라벨 → (실명, 출처)
        for lb in {s.speaker for s in clova.segments}:
            entry = g.get("names", {}).get(lb)
            if isinstance(entry, dict):
                label_gold[lb] = (entry.get("name", lb), entry["source"])
            elif isinstance(entry, str):
                label_gold[lb] = (lb, entry)
            elif not NOT_A_NAME.match(lb):
                label_gold[lb] = (lb, g.get("default", args.default_source))
        if not label_gold:
            continue
        rid = f"R{len(recs) + 1}"
        print(f"[{rid}] 화자분리 {clova.duration / 60:.0f}분 …", file=sys.stderr, flush=True)
        turns, embs, segs = diarize_cached(audio, args.cache)
        secs: dict[str, float] = {}
        for s, e, spk in turns:
            secs[spk] = secs.get(spk, 0.0) + e - s
        by_lb: dict[str, list] = {}
        for s, e, lb, v in segs:
            by_lb.setdefault(lb, []).append((s, e, v))
        rec = Rec(rid, audio, clova, turns, embs, by_lb, secs, label_gold)
        map_clova(rec)
        if args.answers:
            cp, ap = args.answers / f"{audio.stem}.cases.json", args.answers / f"{audio.stem}.answers.json"
            if cp.is_file() and ap.is_file():
                n = apply_answers(rec, json.loads(cp.read_text(encoding="utf-8"))["cases"],
                                  json.loads(ap.read_text(encoding="utf-8")))
                print(f"[{rid}] 판정 반영: 클러스터 {n}개", file=sys.stderr)
        recs.append(rec)
        print(f"[{rid}] {clova.duration / 60:.0f}분, 클러스터 {len(embs)}, 정답 화자 {len(label_gold)}, "
              f"정답 대응 {len(rec.truth)}, 불순 {len(rec.impure)}", file=sys.stderr)
    return recs


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("audio_dir", type=Path)
    p.add_argument("clova_dir", type=Path)
    p.add_argument("--cache", type=Path, default=Path("local/voices-eval"))
    p.add_argument("--pair", action="append", default=[], help="오디오=클로바.txt (이름이 다른 쌍 추가)")
    p.add_argument("--gold", type=Path, help="화자 출처·이름 보정 JSON (로컬 전용)")
    p.add_argument("--default-source", default="clova-manual", choices=SOURCES)
    p.add_argument("--sources", default="clova-manual,user", help="채점에 쓸 정답 출처")
    p.add_argument("--min-sec", type=float, default=voices.MIN_ENROLL_SEC)
    p.add_argument("--enroll", choices=["robust", "centroid", "both"], default="both")
    p.add_argument("--cases-at", type=float, help="이 임계값의 오태깅 사례를 시각과 함께 파일로")
    p.add_argument("--queue", type=Path, help="판정 사례 <stem>.cases.json을 쓸 폴더 (local/ 아래)")
    p.add_argument("--answers", type=Path, help="판정 결과 <stem>.answers.json이 있는 폴더 → gold v1")
    p.add_argument("--verbose", action="store_true", help="클러스터별 이름·점수 (개인정보: 로컬에서만)")
    args = p.parse_args()
    load_dotenv(ENV_FILE)
    args.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    use = set(args.sources.split(","))
    modes = ["robust", "centroid"] if args.enroll == "both" else [args.enroll]

    recs = load_recs(args)
    names = {n for r in recs for n, s in r.truth.values() if s in use}
    recurring = sum(1 for n in names if sum(n in {x for x, _ in r.truth.values()} for r in recs) > 1)
    src_count = Counter(s for r in recs for _, s in r.truth.values() if s in use)
    print(f"gold = {'+'.join(sorted(use))} ({', '.join(f'{k} {v}' for k, v in src_count.items())} 클러스터), "
          f"화자 {len(names)}명(2건 이상 등장 {recurring}명) / 녹취 {len(recs)}건, "
          f"불순으로 뺀 클러스터 {sum(len(r.impure) for r in recs)}개 (순도 기준 {PURITY:.0%})")
    if args.verbose:
        for r in recs:
            people = build_people(recs, r.rid, use, args.min_sec, modes[0])
            labels, pnames, m = voices.score_matrix(people, clusters_of(r, use))
            for i, lb in enumerate(labels):
                top = sorted(zip(m[i], pnames), reverse=True)[:2]
                print(f"  {r.rid} {lb} {r.secs.get(lb, 0):6.0f}s 정답={r.truth.get(lb, ('-',))[0]} "
                      + " ".join(f"{n}:{s:.2f}" for s, n in top), file=sys.stderr)
            for lb, why in r.impure.items():
                print(f"  {r.rid} {lb} 불순: {why}", file=sys.stderr)

    for mode in modes:
        print_table(recs, use, args.min_sec, mode)
    if args.cases_at is not None:
        _, cases = evaluate(recs, use, args.min_sec, modes[0], args.cases_at)
        path = args.cache / "mistag-cases.md"
        write_mistag_cases(path, cases, args.cases_at)
        print(f"\n사례 {len(cases)}건 → {path}", file=sys.stderr)
    if args.queue:
        write_queue(args.queue, recs, use, args.min_sec, modes[0], args.cases_at or voices.THRESHOLD)


if __name__ == "__main__":
    main()
