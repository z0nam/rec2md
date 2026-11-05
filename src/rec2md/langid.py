"""`--language auto`: 녹음의 언어를 몇 개 창에서 판별한다.

말소리 구간(화자분리 또는 소리 크기로 찾은 것)을 이어 붙인 시간축에서 처음·중간·끝 30초를 잘라 창마다
Whisper 언어 판별 확률을 보고, 그 창을 언어를 정하지 않고 받아 적은 글자(한글·라틴 비율)로 교차 확인한다.
창마다 판정을 모아 녹음 하나를 ko / en / mixed / silent로 정한다.

- mixed(창마다 다른 언어)와 silent(판단할 말소리 없음)는 FALLBACK(ko)으로 전사하고 meta에 그대로 남긴다.
- 확률이 WEAK 미만인 창은 확실한 창이 하나라도 있으면 판정에서 뺀다. 백로그 162건 실측(2026-11-04)에서
  처음·끝의 낮은 확률 en 창은 대부분 잡음 오판이었고, 이 기준으로 mixed 18건 중 8건이 ko가 됐다(en 8건은 그대로).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

from .audio import SAMPLE_RATE

WIN = 30.0          # 창 길이(초). Whisper 언어 판별은 30초 창 하나를 본다
SILENT_RMS = 0.003  # 창 전체가 이보다 조용하면 판정하지 않는다
SURE = 0.9          # 받아 적은 글자가 너무 적은 창은 확률이 이 이상일 때만 믿는다
WEAK = 0.6          # 확실한 창이 있으면 확률이 이보다 낮은 창은 뺀다
MIN_CHARS = 10      # 한글+라틴 글자가 이보다 적으면 글자로 교차 확인할 수 없다
FALLBACK = "ko"

Span = tuple[float, float]
# (창 오디오) → (언어별 확률, 언어를 정하지 않고 받아 적은 글자)
Detector = Callable[[np.ndarray], tuple[dict[str, float], str]]


def window_spans(regions: Sequence[Span], win: float = WIN) -> list[list[Span]]:
    """말소리 구간을 이어 붙인 시간축의 처음·중간·끝 창. 창마다 원래 시각의 조각 목록."""
    regions = [(s, e) for s, e in regions if e > s]
    total = sum(e - s for s, e in regions)
    if total <= 0:
        return []
    starts = sorted({0.0, max(0.0, total / 2 - win / 2), max(0.0, total - win)})
    out = []
    for p in starts:
        pieces, pos = [], 0.0
        for s, e in regions:   # 이어 붙인 시간축의 [p, p+win]이 원래 어느 구간에 걸치는지
            lo, hi = max(p, pos), min(p + win, pos + e - s)
            if hi > lo:
                pieces.append((s + lo - pos, s + hi - pos))
            pos += e - s
            if pos >= p + win:
                break
        out.append(pieces)
    return out


def script_of(text: str) -> str:
    """받아 적은 글자의 문자 체계: ko / en / mix / few."""
    hangul = sum("가" <= c <= "힣" for c in text)
    latin = sum(c.isascii() and c.isalpha() for c in text)
    if hangul + latin < MIN_CHARS:
        return "few"
    return "ko" if hangul > 2 * latin else "en" if latin > 3 * hangul else "mix"


def window_lang(probs: dict[str, float], text: str) -> tuple[str, float, str]:
    """(창의 언어 또는 "?", 1위 확률, "1위언어/글자체계")."""
    top = max(probs, key=probs.get)
    p = float(probs[top])
    script = script_of(text)
    if script == "few":
        lang = top if p >= SURE else "?"
    elif script == top:
        lang = top
    else:   # 글자가 확률과 다르면 글자를 믿는다. 섞였으면 판단을 미룬다
        lang = "?" if script == "mix" else script
    return lang, p, f"{top}/{script}"


def decide(windows: list[dict]) -> tuple[str, str]:
    """창 판정 [{"lang", "prob"}] → (판정 ko|en|…|mixed|silent, 전사에 쓸 언어)."""
    voted = [w for w in windows if w["lang"] not in ("silent", "?")]
    strong = [w for w in voted if w["prob"] >= WEAK]
    langs = {w["lang"] for w in (strong or voted)}
    if not langs:
        return "silent", FALLBACK
    if len(langs) > 1:
        return "mixed", FALLBACK
    lang = langs.pop()
    return lang, lang


def detect(audio: np.ndarray, regions: Sequence[Span] | None, detector: Detector) -> dict:
    """16kHz mono 오디오와 말소리 구간 → meta["language_detect"]에 남길 dict.

    {"verdict", "language", "windows": [{"at", "lang", "prob", "detail"}]}. at은 창의 첫 조각 시작(초, 오디오 기준).
    """
    if regions is None:
        regions = [(0.0, len(audio) / SAMPLE_RATE)]
    return detect_spans(regions, lambda s, e: audio[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)], detector)


def detect_spans(regions: Sequence[Span], read: Callable[[float, float], np.ndarray], detector: Detector) -> dict:
    """detect()와 같되 오디오를 read(start, end)로 필요한 조각만 읽는다(batch --dry-run이 원본에서 바로 읽음)."""
    windows = []
    for pieces in window_spans(regions):
        clip = np.concatenate([read(s, e) for s, e in pieces])
        at = round(pieces[0][0], 1)
        if not clip.size or float(np.sqrt(np.mean(clip ** 2))) < SILENT_RMS:
            windows.append({"at": at, "lang": "silent", "prob": 0.0, "detail": "-"})
            continue
        probs, text = detector(clip.astype(np.float32))
        lang, p, detail = window_lang(probs, text)
        windows.append({"at": at, "lang": lang, "prob": round(p, 3), "detail": detail})
    verdict, language = decide(windows)
    return {"verdict": verdict, "language": language, "windows": windows}


def summary(det: dict) -> str:
    """로그·dry-run 한 줄: `en` 또는 `mixed→ko (ko 0.94, en 1.00, ko 0.93)`."""
    wins = ", ".join(f"{w['lang']} {w['prob']:.2f}" if w["lang"] != "silent" else "무음" for w in det["windows"])
    head = det["verdict"] if det["verdict"] == det["language"] else f"{det['verdict']}→{det['language']}"
    return f"{head} ({wins})" if wins else head


# ---- 구간별 언어 (`--mixed-language per-region`) ----

REGION_SURE = 0.8   # 청크가 FALLBACK이 아닌 언어로 바뀌려면 그 언어 확률이 이 이상이어야 한다
RUN_GAP = 5.0       # 같은 언어 청크 사이 공백이 이 이하면 구간 하나로 합친다
REGION_MIN_SEC = 3.0  # 이보다 짧은 청크는 판별이 불안정해 바로 앞 청크의 언어를 따른다


class RegionPicker:
    """청크마다 언어를 고른다. 후보는 녹음 판별에서 확실하게 나온 언어로 좁힌다(잡음 청크가 엉뚱한 언어로 가지 않게).

    engine.transcribe(language=picker)로 넘기면 청크마다 picker(clip, start, end)가 불린다.
    고른 결과는 runs에 [시작, 끝, 언어]로 쌓인다(이어진 같은 언어는 합침).
    """

    def __init__(self, probs_fn: Callable[[np.ndarray], dict[str, float]], candidates: Sequence[str],
                 fallback: str = FALLBACK, sure: float = REGION_SURE, min_sec: float = REGION_MIN_SEC):
        self.probs_fn, self.fallback, self.sure, self.min_sec = probs_fn, fallback, sure, min_sec
        self.candidates = list(dict.fromkeys([fallback, *candidates]))
        self.runs: list[list] = []
        self.sec: dict[str, float] = {}
        self._prev = fallback

    def pick(self, clip: np.ndarray, start: float, end: float) -> str:
        if end - start < self.min_sec:
            lang = self._prev
        else:
            probs = self.probs_fn(clip.astype(np.float32))
            best = max(self.candidates, key=lambda c: probs.get(c, 0.0))
            lang = best if best == self.fallback or probs.get(best, 0.0) >= self.sure else self.fallback
        self._prev = lang
        self.sec[lang] = self.sec.get(lang, 0.0) + end - start   # 실제로 전사한 길이만 센다
        # 같은 언어라도 RUN_GAP보다 긴 말소리 공백을 사이에 두면 따로 둔다(전사하지 않은 구간을 덮지 않게)
        if self.runs and self.runs[-1][2] == lang and start - self.runs[-1][1] <= RUN_GAP:
            self.runs[-1][1] = round(end, 1)
        else:
            self.runs.append([round(start, 1), round(end, 1), lang])
        return lang

    __call__ = pick

    def meta(self) -> dict:
        """meta["language_detect"]["regions"]: 언어별 초와 이어진 구간 목록(시각은 전사한 오디오 기준)."""
        return {"sec": {k: round(v, 1) for k, v in self.sec.items()}, "runs": self.runs}


def candidates_of(det: dict) -> list[str]:
    """녹음 판별의 창 중 확실한 것(WEAK 이상)에서 나온 언어들."""
    return list(dict.fromkeys(w["lang"] for w in det["windows"]
                              if w["lang"] not in ("silent", "?") and w["prob"] >= WEAK))


def split_long(regions: Sequence[Span], audio: np.ndarray, max_len: float = 25.0, search: float = 8.0,
               frame: float = 0.1) -> list[Span]:
    """max_len보다 긴 청크를 [max_len - search, max_len] 안에서 가장 조용한 0.1초 지점에서 나눈다.

    화자분리는 한 사람이 쉬지 않고 이어 말한 구간을 청크 하나로 남긴다(발표 등, 몇 분짜리도 있음). 청크 언어는 청크
    첫 30초로 정해지므로, 구간별 언어를 쓸 때만 긴 청크를 나눠 중간에 바뀐 언어를 놓치지 않게 한다."""
    n = int(frame * SAMPLE_RATE)
    out: list[Span] = []
    for s, e in regions:
        while e - s > max_len:
            lo, hi = int((s + max_len - search) * SAMPLE_RATE), int((s + max_len) * SAMPLE_RATE)
            seg = audio[lo:hi]
            k = len(seg) // n
            if k == 0:
                break
            rms = np.sqrt((seg[:k * n].reshape(k, n) ** 2).mean(axis=1))
            cut = (lo + int(np.argmin(rms)) * n + n // 2) / SAMPLE_RATE
            out.append((s, cut))
            s = cut
        out.append((s, e))
    return out
