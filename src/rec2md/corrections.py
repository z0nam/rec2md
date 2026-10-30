"""보정 후보 추출: segments + 맥락(용어집) → <이름>.corrections.json

CLI는 고치지 않는다. 고칠 만한 구간과 근거만 모으고, 실제 교정은 에이전트가 후보 구간에 한해 한다
(전체를 다시 쓰게 하면 LLM이 멀쩡한 말까지 바꾼다).

후보가 되는 구간:
- 용어집의 `오인식 => 정답` 매핑과 똑같은 표기
- 용어집·참석자·자료 후보와 자모 편집거리가 가까운데 똑같지는 않은 표기
- 엔진이 단어 확률을 낮게 준 구간(low_conf). 가까운 용어가 있으면 함께 제안한다
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .render import fmt_ts
from .segments import Transcript, low_conf_spans
from .series import Context, _strip_josa

_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"
# 확률만 낮고 가까운 용어가 없는 구간은 이보다 낮을 때만 후보로 올린다. 0.5로 두면 50분 회의에 500건이 넘어
# 에이전트가 다 볼 수 없다(실측). segments.json의 low_conf는 그대로 0.5 기준으로 남긴다
CAND_PROB = 0.2
MAX_NGRAM = 3  # 띄어쓰기로 갈라진 용어("국가 자원 종합 계획")를 잡으려고 어절을 최대 3개까지 붙여 본다


def jamo(s: str) -> str:
    out = []
    for ch in s:
        c = ord(ch) - 0xAC00
        if 0 <= c < 11172:
            out += [_CHO[c // 588], _JUNG[c % 588 // 28], _JONG[c % 28].strip()]
        else:
            out.append(ch.lower())
    return "".join(out)


def edit_distance(a: str, b: str, cap: int) -> int:
    """레벤슈타인 거리. cap을 넘으면 cap+1을 돌려주고 일찍 끝낸다."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _key(s: str) -> str:
    return re.sub(r"[^\w]", "", s)


def max_dist(term_key: str, term_jamo: str) -> int:
    """이 용어와 같은 말로 볼 최대 자모거리. 0이면 근사 비교를 하지 않는다(정확한 표기·오인식 매핑만).

    2음절 한글(도청↔도정)과 짧은 약어(AX↔AI)는 한 글자 차이가 흔한 다른 낱말이라 뺀다.
    """
    hangul = sum("가" <= ch <= "힣" for ch in term_key)
    if hangul < 3 and len(term_key) <= 4:
        return 0
    # 자모 4개당 1개까지 틀려도 같은 말로 본다 (3음절 이름 ≈ 자모 8개 → 2)
    return max(1, len(term_jamo) // 4)


class Matcher:
    def __init__(self, vocab: list[str]):
        keys = [(t, _key(t), jamo(_key(t))) for t in vocab if len(_key(t)) >= 2]
        self.exact = {k for _, k, _ in keys}
        self.vocab = [(t, k, j, max_dist(k, j)) for t, k, j in keys]

    def has_exact(self, s: str) -> bool:
        # 맞는 용어가 그대로 들어 있거나 글자가 붙었을 뿐인 어절("3국제자유도시", "그핵심사업")은 고칠 게 없다
        k = _key(s)
        return any(e in k for e in self.exact)

    def near(self, s: str, raw: str | None = None) -> list[tuple[str, int]]:
        """s와 가깝지만 같지 않은 용어 [(용어, 자모거리)], 가까운 순.

        raw는 조사를 떼기 전 어절이다. 용어 자체가 조사꼴로 끝나면("전문가") 떼고 난 "전문"만 보고
        후보로 올리게 되므로, 뗀 쪽과 원래 쪽 어느 하나라도 정확히 맞으면 후보가 아니다.
        """
        k = _key(s)
        if len(k) < 2 or self.has_exact(k) or (raw is not None and self.has_exact(raw)):
            return []
        js = jamo(k)
        out = []
        for term, tk, tj, cap in self.vocab:
            if not cap or abs(len(tk) - len(k)) > 1:
                continue
            d = edit_distance(js, tj, cap)
            if 0 < d <= cap:
                out.append((term, d))
        return sorted(out, key=lambda x: x[1])


def _raw_spans(text: str) -> list[tuple[str, str]]:
    """어절 1〜MAX_NGRAM개 묶음 [(조사 뗀 것, 원래 것)]."""
    toks = [t for t in re.split(r"\s+", text) if t]
    out = []
    for n in range(1, MAX_NGRAM + 1):
        for i in range(len(toks) - n + 1):
            raw = _key("".join(toks[i:i + n]))
            out.append((_strip_josa(raw), raw))
    return out


def _spans(text: str) -> list[str]:
    """어절 1〜MAX_NGRAM개 묶음. 마지막 어절의 조사는 뗀다."""
    return [sp for sp, _ in _raw_spans(text)]


def find_candidates(t: Transcript, ctx: Context) -> list[dict]:
    m = Matcher(ctx.vocabulary())
    variants = {_key(w): r for w, r in ctx.variants.items()}
    cands: list[dict] = []
    for s in t.segments:
        found: dict[str, dict] = {}  # 표기 → 후보 (같은 세그먼트 안 중복 제거)

        def add(text: str, suggest: list[str], reason: str, ts: float, conf: float | None = None):
            c = found.setdefault(_strip_josa(_key(text)), {
                "ts": round(ts, 2), "at": fmt_ts(ts), "speaker": s.speaker, "text": text,
                "suggest": [], "reasons": [], "context": s.text, "fix": None})
            c["suggest"] = list(dict.fromkeys(c["suggest"] + suggest))
            if reason not in c["reasons"]:
                c["reasons"].append(reason)
            if conf is not None:
                c["prob"] = conf

        body = _key(s.text)
        for w, r in variants.items():
            if w and w in body:
                add(w, [r], "용어집 오인식 매핑", s.start)
        for sp, raw in _raw_spans(s.text):
            for term, d in m.near(sp, raw)[:3]:
                add(sp, [term], f"용어 '{term}' 자모거리 {d}", s.start)
        for st, _en, p, text in s.low_conf or low_conf_spans(s.words):
            near = [term for sp, raw in _raw_spans(text) for term, _ in m.near(sp, raw)[:2]]
            if near or p < CAND_PROB or _strip_josa(_key(text)) in found:
                add(text, near, f"낮은 확률 {p}", st, p)
        cands += found.values()
    # 같은 위치 후보끼리는 근거가 많은 쪽이 먼저 보이게
    return sorted(cands, key=lambda c: (c["ts"], -len(c["reasons"])))


def write_corrections(t: Transcript, ctx: Context, path: Path, series: str | None = None) -> int:
    cands = find_candidates(t, ctx)
    data = {"source": t.source, "series": series, "note": "fix는 에이전트가 채운다. 확신이 없으면 [?]를 붙인다",
            "candidates": cands}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(cands)
