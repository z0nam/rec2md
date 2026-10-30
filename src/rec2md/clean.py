"""엔진 공통 환각 정리."""

from __future__ import annotations

import re

from .segments import Segment

MAX_REPEAT = 3
# 디코딩 루프는 단어가 붙어서 나온다. 이보다 길게 끊겼으면 새 발화로 보고 다시 센다
LOOP_GAP = 1.0


def _norm(t: str) -> str:
    return re.sub(r"[\s.,!?…]+", "", t)


def collapse_repeats(segs: list[Segment]) -> list[Segment]:
    """같은 토큰이 MAX_REPEAT번 넘게 연달아 나오면 잘라낸다 ("초. 초. 초. 초. …" 같은 디코딩 루프).

    사람도 "네 네 네"는 말하므로 3번까지는 남긴다. 세그먼트 경계를 넘어 이어지는 반복도 센다.
    """
    prev, prev_end, count = None, None, 0
    out = []
    for s in segs:
        if not s.words:
            out.append(s)
            continue
        kept = []
        for w in s.words:
            key = _norm(w.text)
            gap = w.start - prev_end if prev_end is not None else 0.0
            count = count + 1 if key and key == prev and gap <= LOOP_GAP else 1
            prev, prev_end = key, w.end
            if count <= MAX_REPEAT:
                kept.append(w)
        if kept:
            s.words = kept
            s.text = "".join(w.text for w in kept).strip()
            s.start, s.end = kept[0].start, kept[-1].end
            out.append(s)
    return out


MAX_PERIOD = 4  # 몇 토큰짜리 묶음까지 반복 루프로 볼지


def collapse_ngram_loops(segs: list[Segment]) -> list[Segment]:
    """2〜MAX_PERIOD개 토큰 묶음이 MAX_REPEAT번 넘게 이어지면 MAX_REPEAT번만 남긴다.

    collapse_repeats는 같은 토큰의 연속만 보므로 "가나 bar, 가나 bar, …"처럼 두 토큰이 번갈아 도는
    루프를 놓친다(실측: 64분 회의에서 한 세그먼트가 55회 반복, 회의 뒤 구간 CER 35%→53%).
    한 세그먼트 안에서만 본다. 같은 토큰 하나로 된 묶음은 collapse_repeats 몫이다.
    단어 사이가 LOOP_GAP보다 벌어지면 반복 세기를 끊는다. 쉬었다가 같은 말을 다시 한 것은 진짜 발화다.
    """
    for s in segs:
        if not s.words:
            continue
        words = s.words

        def tight(a: int, b: int) -> bool:  # words[a:b]가 끊김 없이 붙어 있는가
            return all(words[j].start - words[j - 1].end <= LOOP_GAP for j in range(max(a, 1), b))

        keys = [_norm(w.text) for w in words]
        drop = [False] * len(keys)
        for p in range(2, MAX_PERIOD + 1):
            i = 0
            while i + p * (MAX_REPEAT + 1) <= len(keys):
                unit = keys[i:i + p]
                if not all(unit) or len(set(unit)) == 1 or any(drop[i:i + p]) or not tight(i + 1, i + p):
                    i += 1
                    continue
                k = 1
                while keys[i + k * p:i + (k + 1) * p] == unit and tight(i + k * p, i + (k + 1) * p):
                    k += 1
                if k > MAX_REPEAT:
                    for j in range(i + MAX_REPEAT * p, i + k * p):
                        drop[j] = True
                    i += k * p
                else:
                    i += 1
        if any(drop):
            s.words = [w for w, d in zip(s.words, drop) if not d]
            s.text = "".join(w.text for w in s.words).strip()
            s.end = s.words[-1].end
    return segs


# Whisper가 무음·잡음에서 자주 지어내는 문구(방송 자막으로 학습된 흔적). 세그먼트가 이것뿐이면 버린다
_KNOWN = [
    "시청해주셔서감사합니다", "구독과좋아요", "구독좋아요", "촬영기자", "영상편집", "자막제공",
    "MBC뉴스", "KBS뉴스", "SBS뉴스", "뉴스를마칩니다", "다음영상에서만나요",
]


def _prompt_echo(t: str, prompt_items: list[str]) -> bool:
    """세그먼트가 프롬프트 항목만 나열한 것인가 (무음 구간에서 initial_prompt가 그대로 튀어나오는 환각).

    항목 하나만 말한 짧은 발화("홍길동")는 진짜일 수 있으므로 항목 두 개 이상이 이어진 경우만 본다.
    """
    hits = 0
    for item in sorted(prompt_items, key=len, reverse=True):
        k = _norm(item)
        if k and k in t:
            t = t.replace(k, "", 1)
            hits += 1
    return hits >= 2 and not t


def drop_known_hallucinations(segs: list[Segment], prompt: str | None = None) -> list[Segment]:
    items = [x.strip() for x in prompt.split(",")] if prompt else []

    def bogus(s: Segment) -> bool:
        t = _norm(s.text)
        return (len(t) <= 20 and any(k in t for k in _KNOWN)) or (bool(items) and _prompt_echo(t, items))

    return [s for s in segs if not bogus(s)]
