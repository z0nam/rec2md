"""회의 메모 ↔ 녹취 정렬: 메모 문단마다 녹취 근거 구간 후보를 붙인다 → <이름>.notes-align.json

CLI는 대조 판단을 하지 않는다. 문단별 근거 후보, 녹취에 아예 없는 숫자·용어, 어느 문단도 가리키지 않는
녹취 구간(메모에서 빠졌을 수 있는 논의)만 모은다. 빠진 결정·틀린 숫자·발언자 판단과 보정본 작성은
에이전트가 한다(SKILL.md "4. 회의 메모 보정").

정렬은 키워드·숫자·인명 겹침 검색이다(임베딩 없음). 녹취를 일정 길이 창으로 자르고, 메모 문단의 낱말이
창 본문에 들어 있으면 IDF 가중으로 점수를 준다. 숫자와 인명·용어는 가중을 더 준다.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .render import fmt_ts
from .segments import Transcript
from .series import _STOP, _TOKEN, _strip_josa, doc_text

WINDOW_SEC = 30.0
TOP_K = 3
MIN_SCORE = 1.0      # 이보다 낮은 창은 근거 후보로 내지 않는다
UNCOVERED_SEC = 20.0
COVER_RATIO = 0.75   # 1순위 점수의 이 비율 이상인 후보만 "메모가 다룬 구간"으로 친다  # 어느 문단도 가리키지 않는 녹취 구간이 이보다 길면 보고한다

W_NUMBER, W_NAME, W_WORD = 3.0, 2.5, 1.0

# 메모 줄 앞의 목록·번호·제목 기호
_LEAD = re.compile(r"^\s*(?:#{1,6}\s+|[-*+·•▶▷○●◦□■※>]\s*|\(?\d{1,2}[.)]\s+|[가-하][.)]\s+)+")
# 숫자는 바로 뒤 단위 한 글자까지 묶는다("3억", "4월", "15일", "30%"). "2차"의 2가 "2억"에 걸리지 않게
_NUM = re.compile(r"\d+(?:[.,]\d+)*(?:%|[가-힣])?")


@dataclass
class Window:
    start: float
    end: float
    text: str
    speakers: list[str]


@dataclass
class Candidate:
    start: float
    end: float
    ts: str
    score: float
    matched: list[str]
    speakers: list[str]
    text: str


@dataclass
class Paragraph:
    idx: int
    text: str
    kind: str = "text"   # heading(메모의 제목 줄) | text
    numbers: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    words: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 녹취 어디에도 없는 숫자·인명·용어
    candidates: list[Candidate] = field(default_factory=list)


# ---- 메모 ----

def memo_paragraphs(path: Path) -> list[str]:
    """메모 → 문단(줄) 목록. 목록 기호·번호를 떼고, 표는 칸을 공백으로 잇는다."""
    return [text for text, _ in memo_lines(path)]


def memo_lines(path: Path) -> list[tuple[str, str]]:
    """(문단, 종류). md 제목(#)은 heading."""
    out = []
    for line in doc_text(path).splitlines():
        line = line.strip()
        if not line or re.fullmatch(r"[|\-:\s]+", line):  # 빈 줄, md 표 구분선
            continue
        kind = "heading" if line.startswith("#") else "text"
        line = " ".join(c.strip() for c in line.strip("|").split("|")) if line.startswith("|") else line
        line = _LEAD.sub("", line).strip()
        if line:
            out.append((line, kind))
    return out


def norm_num(s: str) -> str:
    return s.replace(",", "")


def tokens(text: str, names: set[str]) -> tuple[list[str], list[str], list[str]]:
    """(숫자, 인명·용어, 일반 낱말). 낱말은 조사를 떼고 2자 이상, 불용어 제외."""
    nums = list(dict.fromkeys(norm_num(m) for m in _NUM.findall(text)))
    hit_names = [n for n in names if n and n in text]
    words = []
    for tok in _TOKEN.findall(text):
        if tok[0].isdigit():  # 숫자는 위에서 단위와 함께 셌다
            continue
        w = _strip_josa(tok)
        if w.endswith("으") and tok.endswith("으로"):  # "원으로" → "원으"가 아니라 "원"
            w = w[:-1]
        if len(w) < 2 or w in _STOP or any(w in n for n in hit_names):
            continue
        words.append(w)
    return nums, hit_names, list(dict.fromkeys(words))


# ---- 녹취 ----

def speaker_name(spk: str | None, names: dict) -> str:
    d = names.get(spk) if spk else None
    if isinstance(d, dict) and d.get("name"):
        return d["name"] + ("?" if d.get("tentative") and d.get("source") != "user" else "")
    if isinstance(d, str):
        return d
    return spk or ""


def windows(t: Transcript, size: float = WINDOW_SEC) -> list[Window]:
    """size초 창, 절반씩 겹치게. 창 경계는 세그먼트 경계를 따른다."""
    segs = [s for s in t.segments if s.text.strip()]
    names = t.meta.get("speaker_names") or {}
    out: list[Window] = []
    i = 0
    while i < len(segs):
        j = i
        while j < len(segs) and segs[j].end - segs[i].start <= size:
            j += 1
        j = max(j, i + 1)
        chunk = segs[i:j]
        out.append(Window(chunk[0].start, chunk[-1].end, " ".join(s.text.strip() for s in chunk),
                          list(dict.fromkeys(speaker_name(s.speaker, names) for s in chunk if s.speaker))))
        if j >= len(segs):
            break
        # 다음 창은 이 창의 중간쯤에서 시작
        mid = chunk[0].start + (chunk[-1].end - chunk[0].start) / 2
        nxt = next((k for k in range(i + 1, j) if segs[k].start >= mid), j)
        i = nxt
    return out


def _has(w: Window, key: str, kind: str) -> bool:
    if kind == "num":  # 숫자는 앞이 다른 숫자로 이어지지 않을 때만 ("3억"이 "13억"에 걸리지 않게)
        tail = "" if not key[-1].isdigit() else r"(?![\d])"
        return re.search(rf"(?<![\d.,]){re.escape(key)}{tail}", w.text.replace(",", "")) is not None
    if kind == "name" and key in {s.rstrip("?") for s in w.speakers}:  # 인명은 발화자로 나와도 근거
        return True
    return key in w.text


# ---- 정렬 ----

def align(paras: list[str] | list[tuple[str, str]], t: Transcript, names: set[str] | None = None, top_k: int = TOP_K,
          window: float = WINDOW_SEC) -> tuple[list[Paragraph], list[dict]]:
    """메모 문단마다 근거 후보 창 top_k개. 두 번째 값은 어느 문단도 가리키지 않은 녹취 구간.
    paras는 문단 목록 또는 memo_lines()의 (문단, 종류) 목록."""
    names = set(names or ())
    names |= {d["name"] for d in (t.meta.get("speaker_names") or {}).values()
              if isinstance(d, dict) and d.get("name")}
    wins = windows(t, window)
    n = len(wins)

    df: Counter[str] = Counter()
    keyed: list[Paragraph] = []
    if paras and isinstance(paras[0], tuple):
        kinds_of, paras = [k for _, k in paras], [t for t, _ in paras]
    else:
        kinds_of = ["text"] * len(paras)
    for i, text in enumerate(paras):
        nums, nms, words = tokens(text, names)
        keyed.append(Paragraph(i, text, kinds_of[i], nums, nms, words))
    kinds = {k: "word" for p in keyed for k in p.words} | {k: "name" for p in keyed for k in p.names} \
        | {k: "num" for p in keyed for k in p.numbers}
    for key, kind in kinds.items():
        df[key] = sum(_has(w, key, kind) for w in wins)

    def idf(k: str) -> float:
        return math.log((n + 1) / (df[k] + 0.5))

    covered: list[tuple[float, float]] = []
    for p in keyed:
        weighted = ([(k, W_NUMBER, "num") for k in p.numbers] + [(k, W_NAME, "name") for k in p.names]
                    + [(k, W_WORD, "word") for k in p.words])
        p.missing = [k for k, _, kind in weighted if df[k] == 0 and (kind != "word" or len(k) >= 3)]
        scored = []
        for w in wins:
            hit = [(k, wt) for k, wt, kind in weighted if df[k] and _has(w, k, kind)]
            score = sum(wt * idf(k) for k, wt in hit)
            if score >= MIN_SCORE:
                scored.append((score, w, [k for k, _ in hit]))
        scored.sort(key=lambda x: (-x[0], x[1].start))
        best = _dedupe(scored)[:top_k]
        for score, w, hit in best:
            p.candidates.append(Candidate(w.start, w.end, fmt_ts(w.start), round(score, 2), hit, w.speakers, w.text))
            # "메모가 다룬 구간"은 1순위와 그에 버금가는 후보만, 그리고 두 낱말 이상 겹친 것만 친다.
            # 약한 2·3순위까지 치면 메모에서 빠진 논의가 가려진다
            if score >= COVER_RATIO * best[0][0] and len(hit) >= 2:
                covered.append((w.start, w.end))
    return keyed, uncovered(wins, covered)


def _dedupe(scored: list) -> list:
    """겹치는 창은 점수 높은 것 하나만 (반씩 겹쳐 자르므로 같은 발언이 두 번 나오기 쉽다)."""
    out = []
    for s in scored:
        if all(s[1].end <= o[1].start or s[1].start >= o[1].end for o in out):
            out.append(s)
    return out


def uncovered(wins: list[Window], covered: list[tuple[float, float]], min_sec: float = UNCOVERED_SEC) -> list[dict]:
    """어느 문단의 근거 후보에도 걸리지 않은 녹취 구간을 이어 붙여, min_sec 이상인 것만."""
    spans: list[list] = []
    for w in wins:
        if any(not (w.end <= a or w.start >= b) for a, b in covered):
            continue
        if spans and w.start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], w.end)
            spans[-1][2].append(w)
        else:
            spans.append([w.start, w.end, [w]])
    out = []
    for a, b, ws in spans:
        if b - a >= min_sec:
            text, speakers = [], []
            for w in ws:
                text.append(w.text)
                speakers += w.speakers
            out.append({"start": a, "end": b, "ts": fmt_ts(a), "speakers": list(dict.fromkeys(speakers)),
                        "text": _join_overlapping(text)})
    return out


def _join_overlapping(texts: list[str]) -> str:
    """반씩 겹친 창 본문을 이을 때 앞 창과 겹치는 앞부분을 뺀다."""
    out = ""
    for t in texts:
        if not out:
            out = t
            continue
        k = next((k for k in range(min(len(out), len(t)), 0, -1) if out.endswith(t[:k])), 0)
        out += ("" if k else " ") + t[k:]
    return out


def write_align(memo: Path, seg_path: Path, out: Path, names: set[str] | None = None, top_k: int = TOP_K,
                window: float = WINDOW_SEC) -> dict:
    t = Transcript.from_json(seg_path)
    paras, unc = align(memo_lines(memo), t, names, top_k, window)
    data = {
        "memo": memo.name, "segments": seg_path.name, "window_sec": window,
        "paragraphs": [asdict(p) for p in paras],
        "uncovered": unc,
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data
