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


# Whisper가 무음·잡음에서 자주 지어내는 문구(방송 자막으로 학습된 흔적). 세그먼트가 이것뿐이면 버린다
_KNOWN = [
    "시청해주셔서감사합니다", "구독과좋아요", "구독좋아요", "촬영기자", "영상편집", "자막제공",
    "MBC뉴스", "KBS뉴스", "SBS뉴스", "뉴스를마칩니다", "다음영상에서만나요",
]


def drop_known_hallucinations(segs: list[Segment]) -> list[Segment]:
    def bogus(s: Segment) -> bool:
        t = _norm(s.text)
        return len(t) <= 20 and any(k in t for k in _KNOWN)

    return [s for s in segs if not bogus(s)]
