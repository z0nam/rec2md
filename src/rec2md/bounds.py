"""회의 구간 추정: 녹음기를 늦게 켜거나 안 꺼서 앞뒤에 붙은 잡담·통화를 회의 범위에서 떼어낼 후보.

CLI는 추정과 근거만 meta["meeting_bounds"]에 남기고, 요약·회의록 범위는 에이전트가 근거를 보고 정한다.

끝 신호(시각 t마다 점수):
- 종료 표현: t 직전 90초 안의 "수고하셨습니다", "들어가십시오", "다음 회의" 등
- 발화 밀도 하락: t 뒤 5분의 말소리 비율이 회의 중 중앙값보다 크게 낮음
- 화자 구성 변화: t 뒤 5분을 한 화자가 독차지함(통화·혼잣말. 회의 중 그 정도로 쏠린 적이 없을 때만),
  말하는 사람 수가 회의 중보다 크게 줄어듦, 회의 중 화자가 아닌 사람들이 대부분을 말함
- 긴 무음: t 뒤 첫 발화까지 30초 넘게 비어 있음
시작은 같은 원리를 거꾸로 쓰되 신호가 약하면 0으로 둔다(회의 전 잡담은 짧고 표현도 일정하지 않다).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass

from .render import fmt_ts
from .segments import Segment, Transcript

WIN = 300.0  # 앞뒤를 비교하는 창(초)
STEP = 10.0  # 후보 시각 간격. 발화마다 다 보면 6시간 녹음에서 수십 초 걸린다
EDGE = 120.0  # 녹음 끝에서 이보다 가까운 종료 후보는 "끝까지 회의"로 본다(창이 짧으면 신호가 흔들린다)
LOOKBACK = 90.0  # 종료 표현을 찾는 범위(t 이전)
MIN_SCORE = 2.0  # 이보다 낮으면 자르지 않는다
# 시작 쪽은 개회 표현이 없으면 더 엄격하게 본다. 회의 초반 발표·인사로 화자 구성이 쏠리는 일이 흔하다(실측 오탐)
MIN_SCORE_START_NO_PHRASE = 3.5
NEAR_BEST = 0.8  # 최고점의 이 비율 이상인 후보 중 가장 바깥(끝은 가장 이른, 시작은 가장 늦은) 시각을 쓴다

# 폐회 선언은 그 자체로 강한 신호, 인사말은 회의 중에도 나오므로 약한 신호
_CLOSE_STRONG = re.compile(r"(이상으로|이것으로).{0,12}(마치|마무리)|(회의|오늘).{0,10}(마치|마무리)하?겠|폐회")
_CLOSE = re.compile(
    _CLOSE_STRONG.pattern
    + r"|수고(하셨|많으셨|많이\s*하셨)|고생(하셨|많으셨)|들어가(십시오|세요)|조심히\s*(가|들어)"
    r"|다음\s*(회의|주|번)에?\s*(뵙|봬|보)|다음\s*회의는|뵙겠습니다")
_OPEN = re.compile(
    r"(회의|회의를|오늘\s*회의)\s*.{0,6}시작하?겠|시작하도록\s*하겠|(그럼|그러면|자)\s*시작(하|할)"
    r"|개회|모두\s*발언|참석해\s*주셔서\s*감사")


def _speech(segs: list[Segment], a: float, b: float) -> Counter:
    c: Counter = Counter()
    for s in segs:
        o = min(b, s.end) - max(a, s.start)
        if o > 0:
            c[s.speaker] += o
    return c


def _density(segs: list[Segment], a: float, b: float) -> float:
    return sum(_speech(segs, a, b).values()) / max(b - a, 1.0)


def _top_share(c: Counter) -> float:
    total = sum(c.values())
    return c.most_common(1)[0][1] / total if total else 0.0


def _phrases(segs: list[Segment], pat: re.Pattern, a: float, b: float) -> list[tuple[float, str]]:
    return [(s.start, m.group(0)) for s in segs if a <= s.start <= b for m in [pat.search(s.text)] if m]


def _gap_after(segs: list[Segment], t: float) -> float:
    nxt = min((s.start for s in segs if s.start >= t), default=None)
    return (nxt - t) if nxt is not None else 0.0


@dataclass
class Baseline:
    density: float  # 5분 창 말소리 비율 중앙값
    top: float  # 5분 창에서 한 화자 쏠림의 최댓값
    active: float  # 5분 창의 활성 화자 수 중앙값
    speakers: set  # 회의 본체에서 2% 이상 말한 화자


def _active(c: Counter) -> int:
    return sum(v > 5 for v in c.values())


def _baseline(segs: list[Segment], a: float, b: float) -> Baseline:
    ds, tops, acts = [], [], []
    t = a
    while t + WIN <= b:
        c = _speech(segs, t, t + WIN)
        ds.append(sum(c.values()) / WIN)
        tops.append(_top_share(c))
        acts.append(_active(c))
        t += WIN / 2
    whole = _speech(segs, a, b)
    total = sum(whole.values()) or 1
    spk = {k for k, v in whole.items() if v / total >= 0.02}
    if not ds:
        return Baseline(total / max(b - a, 1), _top_share(whole), _active(whole), spk)
    return Baseline(statistics.median(ds), max(tops), statistics.median(acts), spk)


def _side_score(segs: list[Segment], a: float, b: float, base: Baseline, gap: float):
    """[a, b](회의 밖으로 의심되는 쪽) 창이 회의 본체와 얼마나 다른가."""
    ev: list[str] = []
    score = 0.0
    c = _speech(segs, a, b)
    dens = sum(c.values()) / max(b - a, 1.0)
    if base.density > 0 and dens < 0.75 * base.density:
        score += 2.0 * (1 - dens / base.density)
        ev.append(f"발화 밀도 {base.density:.0%}→{dens:.0%}")
    top = _top_share(c)
    if c and top > max(0.8, base.top + 0.15):
        score += 1.5 * (top - base.top)
        ev.append(f"{c.most_common(1)[0][0]}가 {top:.0%}를 독차지(회의 중 최대 {base.top:.0%})")
    act = _active(c)
    if c and base.active >= 3 and act <= 2:
        score += 1.0
        ev.append(f"말하는 사람 {act}명(회의 중 {base.active:.0f}명)")
    total = sum(c.values())
    if total > 30:
        known = sum(v for k, v in c.items() if k in base.speakers) / total
        if known < 0.5:
            score += 1.0 * (1 - known)
            ev.append(f"회의 화자 비중 {known:.0%}")
    if gap >= 30:
        score += min(1.0, gap / 120)
        ev.append(f"무음 {gap:.0f}초")
    return score, ev


def _phrase_score(hits: list[tuple[float, str]], strong: re.Pattern | None) -> float:
    return min(2.5, sum(1.5 if strong and strong.search(p) else 1.0 for _, p in hits))


def _quote(hits: list[tuple[float, str]]) -> str:
    return ", ".join(f"[{fmt_ts(ts)}] '{p}'" for ts, p in hits[:3])


def _confidence(score: float, has_phrase: bool) -> str:
    return "high" if score >= 3.5 and has_phrase else "medium"


def _start_threshold(x: tuple) -> float:
    return MIN_SCORE if x[3] else MIN_SCORE_START_NO_PHRASE


def _pick(scored: list[tuple], earliest: bool, threshold) -> tuple:
    """기준을 넘는 후보 중, 최고점 근처에서 회의 쪽에 가장 가까운 것. 창 신호는 경계에서 멀어질수록
    커지는 경향이 있어 최고점만 고르면 경계가 바깥으로 밀린다(실측: 회의 뒤 통화의 끝 무렵이 최고점).

    threshold(후보) → 그 후보가 넘어야 할 점수. 기준 미달 후보를 먼저 빼야 최고점이 기준을 겨우 넘을 때
    80% 규칙이 미달 후보를 골라 경계를 통째로 버리는 일이 없다. 넘는 후보가 없으면 최고점 후보를 돌려준다
    (호출 쪽이 기준과 비교해 "신호 없음"으로 보고한다).
    """
    if not scored:
        return 0.0, None, [], []
    ok = [x for x in scored if x[0] >= threshold(x)]
    if not ok:
        return max(scored, key=lambda x: x[0])
    top = max(x[0] for x in ok)
    near = [x for x in ok if x[0] >= NEAR_BEST * top]
    return min(near, key=lambda x: x[1]) if earliest else max(near, key=lambda x: x[1])


def estimate(t: Transcript) -> dict:
    segs = [s for s in t.segments if s.text.strip()]
    dur = t.duration or (segs[-1].end if segs else 0.0)
    out = {"start": 0.0, "end": round(dur, 1), "confidence": "none", "evidence": []}
    if not segs or dur < 2 * WIN:
        out["evidence"].append("녹음이 짧아 추정하지 않음")
        return out
    # 기준선은 앞뒤 잡담이 섞일 수 있는 양 끝을 빼고 가운데 60%에서 잡는다
    base = _baseline(segs, dur * 0.2, dur * 0.8)
    confs = []

    # 끝: 후반 40%의 발화 끝 시각마다 "그 뒤 5분"을 본다
    scored = []
    last = -STEP
    for s in segs:
        c = s.end
        if c < dur * 0.6 or c > dur - EDGE or c - last < STEP:
            continue
        last = c
        hits = _phrases(segs, _CLOSE, c - LOOKBACK, c + 5)
        side, ev = _side_score(segs, c, min(dur, c + WIN), base, _gap_after(segs, c))
        scored.append((_phrase_score(hits, _CLOSE_STRONG) + side, c, ev, hits))
    sc, end, ev, hits = _pick(scored, earliest=True, threshold=lambda x: MIN_SCORE)
    if sc >= MIN_SCORE:
        if hits:
            # 창 신호는 실제 끝보다 늦게 잡히므로 마지막 종료 표현이 나온 발화의 끝으로 당긴다
            end = max(s.end for s in segs if s.start == hits[-1][0])
            ev = [f"종료 표현 {_quote(hits)}"] + ev
        out["end"] = round(end, 1)
        confs.append(_confidence(sc, bool(hits)))
        out["evidence"].append(f"끝 {fmt_ts(end)} (점수 {sc:.1f}): " + "; ".join(ev))
        tail = [s for s in segs if s.start >= end]
        if tail:
            out["evidence"].append(f"끝 이후 {fmt_ts(dur - end)}, 첫 발화 [{fmt_ts(tail[0].start)}] {tail[0].text[:40]}")
    else:
        out["evidence"].append(f"끝: 뚜렷한 종료 신호 없음(최고 점수 {sc:.1f}) → 녹음 끝까지 회의로 봄")

    # 시작: 전반 40%의 발화 시작 시각마다 "그 앞 5분"을 본다
    scored = []
    last = -STEP
    for s in segs:
        c = s.start
        if c < EDGE or c > dur * 0.4 or c - last < STEP:
            continue
        last = c
        hits = _phrases(segs, _OPEN, c - 5, c + LOOKBACK)
        prev_end = max((x.end for x in segs if x.end <= c), default=0.0)
        side, ev = _side_score(segs, max(0.0, c - WIN), c, base, c - prev_end)
        scored.append((_phrase_score(hits, None) + side, c, ev, hits))
    sc, start, ev, hits = _pick(scored, earliest=False, threshold=_start_threshold)
    if sc >= _start_threshold((sc, start, ev, hits)):
        if hits:
            ev = [f"개회 표현 {_quote(hits)}"] + ev
        out["start"] = round(start, 1)
        confs.append(_confidence(sc, bool(hits)))
        out["evidence"].append(f"시작 {fmt_ts(start)} (점수 {sc:.1f}): " + "; ".join(ev))
    else:
        out["evidence"].append(f"시작: 뚜렷한 신호 없음(최고 점수 {sc:.1f}) → 처음부터 회의로 봄")
    if confs:
        out["confidence"] = "medium" if "medium" in confs else "high"
    return out


def summary(b: dict, duration: float) -> str:
    cut = []
    if b["start"] > 0:
        cut.append(f"앞 {fmt_ts(b['start'])}")
    if b["end"] < duration:
        cut.append(f"뒤 {fmt_ts(duration - b['end'])}")
    return f"{fmt_ts(b['start'])}〜{fmt_ts(b['end'])} ({b['confidence']}" + (f", 제외 후보: {' · '.join(cut)})" if cut else ")")
