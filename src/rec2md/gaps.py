"""전사 누락 의심 구간: 말소리 구간(화자 문단·세그먼트)은 긴데 글자가 파일 평소보다 훨씬 적은 곳.

누락은 조용히 일어난다 — 세그먼트는 시각을 다 덮는데 글자만 비어 있어 "짧게 말했다"처럼 보인다(#58).
글자 밀도(공백 뺀 글자 수 / 초)를 파일 중앙값과 비교해 raw.md·검토 화면에 표식을 남긴다. 고치지는 않는다.

- 화자 문단(같은 화자가 이어 말한 세그먼트, raw.md의 한 단락)이 LONG_BLOCK초 이상이거나, 세그먼트 하나가
  LONG_SEG초 이상인데(Whisper 세그먼트는 보통 30초 이하라 그 자체로 이상하다) 밀도가 중앙값의 RATIO 미만이면 의심.
- 실측(2026-11-20): 누락이 확인된 2시간 회의의 두 구간은 0.6·0.9자/초(중앙값 6.3, 클로바는 정상 분량).
  백로그 녹취 50여 개에 이 기준을 대 보면 회의·강의 녹취는 파일당 0〜1곳, 걸리는 곳 대부분은 소음·사적 대화 녹음이었다.
- 의심 구간이 trim이 잘라낸 무음 구간과 겹치면 그 사실도 남긴다(그때는 `--no-trim` 재전사가 처방이다).
"""

from __future__ import annotations

import statistics

LONG_BLOCK = 120.0  # 화자 문단 길이(초)
LONG_SEG = 60.0     # 세그먼트 하나 길이(초)
RATIO = 0.25        # 파일 중앙값 대비 밀도 비
MIN_LEN = 10.0      # 중앙값 계산에 쓰는 문단 최소 길이(초) — 짧은 맞장구는 밀도가 들쭉날쭉하다
MIN_BLOCKS = 5      # 문단이 이보다 적으면 중앙값을 믿지 않는다


def _chars(text: str) -> int:
    return len("".join(text.split()))


def _blocks(segs) -> list[tuple[float, float, int]]:
    """raw.md와 같은 방식으로 같은 화자 세그먼트를 문단으로 묶는다 → [(시작, 끝, 글자 수)]."""
    out: list[list] = []
    prev = object()
    for s in segs:
        n = _chars(s.text)
        if not n:
            continue
        if out and s.speaker is not None and s.speaker == prev:
            out[-1][1] = max(out[-1][1], s.end)
            out[-1][2] += n
        else:
            out.append([s.start, s.end, n])
        prev = s.speaker
    return [(a, b, n) for a, b, n in out]


def suspects(t) -> list[dict]:
    """[{start, end, cps, median, trim_cut}] — 시작 순, 겹치면 합친다."""
    blocks = _blocks(t.segments)
    dens = [n / (b - a) for a, b, n in blocks if b - a >= MIN_LEN]
    if len(dens) < MIN_BLOCKS:
        return []
    med = statistics.median(dens)
    cand = [(a, b, n) for a, b, n in blocks if b - a >= LONG_BLOCK and n / (b - a) < RATIO * med]
    cand += [(s.start, s.end, _chars(s.text)) for s in t.segments
             if s.end - s.start >= LONG_SEG and _chars(s.text) / (s.end - s.start) < RATIO * med]
    merged: list[list] = []
    for a, b, n in sorted(cand):
        if merged and a < merged[-1][1]:
            m = merged[-1]
            if b > m[1]:  # 겹친 만큼은 글자를 한 번만 센다(세그먼트는 그 문단 안에 있다)
                m[1], m[2] = b, max(m[2], n)
        else:
            merged.append([a, b, n])
    cuts = ((t.meta or {}).get("trim") or {}).get("cut") or []
    return [{"start": round(a, 1), "end": round(b, 1), "cps": round(n / (b - a), 2), "median": round(med, 2),
             "trim_cut": [c for c in cuts if c[0] < b and c[1] > a]}
            for a, b, n in merged]


def note(g: dict) -> str:
    """사람이 읽는 한 줄 사유."""
    why = f"{g['cps']:.1f}자/초(이 파일 중앙값 {g['median']:.1f})"
    if g["trim_cut"]:  # 원인이 분명하다: 그 오디오는 전사기에 들어가지 않았다
        cut = ", ".join(f"{_ts(a)}-{_ts(b)}" for a, b in g["trim_cut"])
        return f"원인: trim으로 잘림({cut}, 전사하지 않은 구간) · {why} → --no-trim으로 다시 전사"
    return f"원인 미상: {why} → 다시 들어 볼 것"


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
