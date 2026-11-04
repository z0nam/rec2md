"""백로그 색인: 전사 출력 폴더(_transcripts/)의 파일마다 기계적 정보 + 에이전트 판단을 한 표로 모은다.

- CLI(build)는 segments.json·batch 상태 파일에서 길이·말소리 길이·회의 구간·화자 수·날짜 단서 후보를 뽑는다.
  날짜 단서는 정규식으로 위치만 찍고 해석하지 않는다(해석은 에이전트 몫).
- 에이전트는 항목마다 `agent` 블록(날짜·정밀도·확신도·근거·주제·참석자 유형·한 줄 요약)을 채운다(`rec2md index set`).
- build를 다시 돌려도 `agent`·`approved`는 덮어쓰지 않는다. 항목은 원본 미디어 이름(segments.json의 source)으로 맞춘다.
  원본을 읽을 수 있으면 지문(크기 + 앞뒤 1MB sha1)을 남기고, 지문이 바뀌면 `stale`로 표시한다
  (녹음기는 이름을 바꾼 파일의 번호를 다시 쓰므로 같은 `RECnnnnn.MP3`가 다른 녹음일 수 있다).
- 이름 바꾸기는 전사 출력만 바꾼다. 미디어는 요청할 때만 새 이름의 사본을 다른 폴더에 만든다. 원본에는 쓰지 않는다.

index.json·index.md·.rec2md-renames.json은 녹취 문맥·실제 파일명을 담으므로 출력 폴더(local)에만 둔다.
날짜 단서(clues)는 발화 문맥을 담아 index.json에만 두고 index.md에는 에이전트의 근거 요약만 쓴다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

from .segments import Transcript

INDEX_JSON, INDEX_MD, RENAMES = "index.json", "index.md", ".rec2md-renames.json"
STATE_FILE = ".rec2md-state.json"
# 이름 바꾸기에서 절대 건드리지 않는 미디어 확장자(cmd_batch.MEDIA_EXTS보다 넓게)
MEDIA_EXTS = {".mp3", ".m4a", ".wav", ".mp4", ".mov", ".aac", ".flac", ".ogg", ".opus", ".wma", ".webm", ".mkv"}  # cmd_batch.STATE_FILE과 같다(순환 import를 피하려고 따로 둠)
SEG_SUFFIX = ".segments.json"
FP_CHUNK = 1 << 20

PRECISIONS = ("day", "month", "year", "unknown")
CONFIDENCES = ("high", "medium", "low", "none")
ATTENDEE_TYPES = ("내부회의", "외부기관", "전화", "강연·발표", "1인 메모", "기타")
AGENT_FIELDS = ("date", "date_precision", "date_confidence", "date_evidence", "topic", "attendee_type",
                "summary", "proposed_name")

REC_RE = re.compile(r"REC(\d{3,})", re.I)
NAME_DATE_RE = re.compile(r"^((?:19|20)\d{6})_")

# 날짜 단서. 해석하지 않고 종류와 위치만 남긴다
CLUE_PATTERNS = [
    ("date", re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")),
    ("year", re.compile(r"((?:19|20)\d{2})\s*년")),
    ("month", re.compile(r"(?<![\d.])(\d{1,2})\s*월(?!\s*\d{1,2}\s*일)(?![가-힣])")),
    ("weekday", re.compile(r"[월화수목금토일]요일")),
    ("relative", re.compile(r"오늘|어제|그저께|그제|내일|모레|(?:다음|지난|이번|다다음)\s*(?:주|달)|올해|작년|내년|금년")),
    ("round", re.compile(r"\d+\s*차\s*(?:회의|회)")),
]
CLUE_CTX = 20      # 단서 앞뒤로 남길 글자 수
MAX_CLUES = 80     # 파일당 상한(긴 녹취에서 "오늘"이 수백 번 나오는 것을 막는다)

WIN_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_STEM_BYTES = 150  # 출력 접미사(.segments.json 등)를 붙여도 macOS 255바이트 안에 들게


# ── 이름 형식: 파일명 표기는 여기 한 곳에서만 정한다 ─────────────────────────

def date_token(date: str | None, precision: str | None) -> str:
    """날짜 + 정밀도 → 파일명 앞 8자. 모르는 자리는 x로 가린다(Windows 파일명에 ?는 못 쓴다).

    day → 20990115, month → 209901xx, year → 2099xxxx, unknown → xxxxxxxx
    """
    digits = re.sub(r"\D", "", date or "")
    keep = {"day": 8, "month": 6, "year": 4}.get(precision or "unknown", 0)
    keep = max((k for k in (8, 6, 4, 0) if k <= min(keep, len(digits))), default=0)  # 날짜가 정밀도보다 짧으면 내린다
    return (digits[:keep] + "x" * 8)[:8]


def sanitize(text: str) -> str:
    """파일명 조각: NFC, Windows 금지 문자·제어 문자 → _, 공백 → _, 끝의 점·공백 제거."""
    s = unicodedata.normalize("NFC", text or "").strip()
    s = WIN_FORBIDDEN.sub("_", s)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("._ ")


def format_name(date: str | None, precision: str | None, topic: str | None, rec: str | None) -> str:
    """제안 이름(확장자 없음): `<날짜8자>_<주제>-RECnnnnn`. 사용자가 이미 쓰는 `YYYYMMDD_제목-RECnnnnn`과 같은 꼴.
    REC 번호는 원본으로 거슬러 찾으려고 남긴다(번호가 재사용될 수 있어 식별자는 아니다)."""
    head = date_token(date, precision)
    body = sanitize(topic or "") or "주제미상"
    tail = f"-{rec}" if rec and rec.upper() not in body.upper() else ""
    budget = MAX_STEM_BYTES - len(head.encode()) - 1 - len(tail.encode())
    while len(body.encode()) > budget:
        body = body[:-1]
    return f"{head}_{body.rstrip('._ ')}{tail}"


# ── build ────────────────────────────────────────────────────────────────

def fingerprint(path: Path) -> str | None:
    """크기 + 앞뒤 1MB의 sha1. 녹음 파일 전체를 읽지 않고 같은 파일인지 가늠한다."""
    try:
        size = path.stat().st_size
        h = hashlib.sha1(str(size).encode())
        with path.open("rb") as f:
            h.update(f.read(FP_CHUNK))
            if size > FP_CHUNK:  # 1〜2MB면 앞 1MB 뒤의 나머지 전부
                f.seek(max(FP_CHUNK, size - FP_CHUNK))
                h.update(f.read(FP_CHUNK))
        return f"{size}:{h.hexdigest()[:16]}"
    except OSError:
        return None


def rec_number(name: str) -> str | None:
    m = REC_RE.search(name)
    return f"REC{m.group(1)}" if m else None


def name_date(name: str) -> str | None:
    """원본 이름이 이미 `YYYYMMDD_…`이면 그 날짜(사람이 붙인 앵커)."""
    m = NAME_DATE_RE.match(unicodedata.normalize("NFC", name))
    if not m:
        return None
    d = m.group(1)
    try:
        dt.date(int(d[:4]), int(d[4:6]), int(d[6:]))
    except ValueError:
        return None
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


WEEKDAY_AFTER = re.compile(r"\s*[,(]?\s*([월화수목금토일])요일")
YEARS_BACK = 8  # 날짜+요일이 맞는 연도를 찾을 범위(올해부터 거슬러)


def weekday_years(month: int, day: int, weekday: str, today: dt.date | None = None) -> list[int]:
    """"N월 N일 X요일"이 맞는 연도(올해 포함 최근 YEARS_BACK년). 7년에 한 번꼴로 맞으므로 대개 1〜2개로 좁혀진다."""
    year = (today or dt.date.today()).year
    want = "월화수목금토일".index(weekday)
    out = []
    for y in range(year, year - YEARS_BACK, -1):
        try:
            if dt.date(y, month, day).weekday() == want:
                out.append(y)
        except ValueError:
            pass
    return out


def find_clues(t: Transcript) -> list[dict]:
    out: list[dict] = []
    for s in t.segments:
        text = s.text or ""
        for kind, pat in CLUE_PATTERNS:
            for m in pat.finditer(text):
                if kind == "date" and not (1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31):
                    continue
                if kind == "month" and not 1 <= int(m.group(1)) <= 12:
                    continue
                a, b = max(0, m.start() - CLUE_CTX), min(len(text), m.end() + CLUE_CTX)
                clue = {"t": round(s.start, 1), "kind": kind, "match": m.group(0), "ctx": text[a:b]}
                if kind == "date" and (w := WEEKDAY_AFTER.match(text, m.end())):
                    clue["weekday"] = w.group(1)
                    clue["years"] = weekday_years(int(m.group(1)), int(m.group(2)), w.group(1))
                out.append(clue)
    # 절대 표현을 먼저 남기고 상대 표현은 뒤로(상한에 걸려도 강한 단서가 남게)
    order = {k: i for i, (k, _) in enumerate(CLUE_PATTERNS)}
    out.sort(key=lambda c: (order[c["kind"]] >= order["weekday"], c["t"]))
    return sorted(out[:MAX_CLUES], key=lambda c: c["t"])


def mechanical(t: Transcript) -> dict:
    """segments.json에서 뽑는 기계적 칸."""
    by_spk: dict[str, float] = {}
    for s in t.segments:
        if s.speaker:
            by_spk[s.speaker] = by_spk.get(s.speaker, 0.0) + max(0.0, s.end - s.start)
    talk = sum(by_spk.values())
    b = t.meta.get("meeting_bounds") or {}
    n = len(t.segments)
    return {
        "duration": round(t.duration, 1),
        "speech_sec": (t.meta.get("trim") or {}).get("kept_sec"),
        "bounds": {k: b.get(k) for k in ("start", "end", "confidence")} if b else None,
        "speakers": len(by_spk),
        "top_share": round(max(by_spk.values()) / talk, 2) if talk else None,
        "segments": n,
        "low_conf_ratio": round(sum(1 for s in t.segments if s.low_conf) / n, 2) if n else None,
        "prompt": bool(t.meta.get("initial_prompt")),
    }


def stem_of(seg: Path) -> str:
    return seg.name[: -len(SEG_SUFFIX)]


def load_index(out: Path) -> dict:
    p = out / INDEX_JSON
    if not p.is_file():
        return {"version": 1, "items": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"색인 파일을 읽을 수 없음: {p}") from None


def save_json(path: Path, data) -> None:
    """임시 파일에 쓰고 바꿔 끼운다(중간에 끊겨도 깨진 파일이 남지 않게)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def media_path(source: str, state: dict, media_dirs: list[Path]) -> Path | None:
    rec = state.get(source)
    cands = [Path(rec["source"])] if isinstance(rec, dict) and rec.get("source") else []
    cands += [d / source for d in media_dirs]
    return next((p for p in cands if p.is_file()), None)


def build(out: Path, media_dirs: list[Path] = ()) -> dict:
    """출력 폴더를 훑어 색인을 만들거나 갱신한다. agent·approved는 보존."""
    old = load_index(out)
    by_source = {i["source"]: i for i in old.get("items", [])}
    state_p = out / STATE_FILE
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.is_file() else {}

    items, seen = [], set()
    for seg in sorted(out.glob(f"*{SEG_SUFFIX}")):
        try:
            t = Transcript.from_json(seg)
        except (OSError, ValueError, TypeError, KeyError) as e:
            print(f"건너뜀(읽기 실패): {seg.name} — {type(e).__name__}")
            continue
        source = t.source or f"{stem_of(seg)}"
        if source in seen:  # 같은 원본을 가리키는 출력이 둘(이름을 손으로 바꾼 사본 등)
            print(f"건너뜀(원본 중복): {seg.name} → {source}")
            continue
        seen.add(source)
        prev = by_source.get(source, {})
        mp = media_path(source, state, list(media_dirs))
        fp = fingerprint(mp) if mp else None
        stale = bool(prev.get("stale"))
        if fp and prev.get("fingerprint") and fp != prev["fingerprint"]:
            stale = True
        item = {
            "source": source,
            "stem": stem_of(seg),
            "rec": rec_number(source),
            "name_date": name_date(source),
            "fingerprint": fp or prev.get("fingerprint"),
            **mechanical(t),
            "clues": find_clues(t),
            "agent": {k: prev.get("agent", {}).get(k) for k in AGENT_FIELDS},
            "approved": bool(prev.get("approved")),
            "stale": stale,
        }
        for k in ("renamed_from",):
            if prev.get(k):
                item[k] = prev[k]
        items.append(item)
    # 이번에 못 찾은 예전 항목(출력을 지웠거나 옮김)은 판단을 잃지 않게 missing으로 남긴다
    for src, prev in by_source.items():
        if src not in seen:
            items.append({**prev, "missing": True})
    _neighbors(items)
    idx = {"version": 1, "updated": dt.datetime.now().isoformat(timespec="seconds"), "items": items}
    save_json(out / INDEX_JSON, idx)
    return idx


def _rec_key(item: dict) -> int | None:
    m = REC_RE.search(item.get("rec") or "")
    return int(m.group(1)) if m else None


def _anchor(item: dict) -> str | None:
    """앵커 날짜: 사람이 붙인 이름의 날짜, 또는 에이전트가 high·day로 정한 날짜."""
    if item.get("name_date"):
        return item["name_date"]
    a = item.get("agent") or {}
    if a.get("date_confidence") == "high" and a.get("date_precision") == "day":
        return a.get("date")
    return None


def _neighbors(items: list[dict]) -> None:
    """REC 번호가 앞뒤인 앵커 파일. 번호가 겹치면(녹음기 번호 재사용) rec_dup으로 표시해 약한 단서로 쓰게 한다."""
    counts: dict[int, int] = {}
    for i in items:
        if (k := _rec_key(i)) is not None:
            counts[k] = counts.get(k, 0) + 1
    anchors = sorted(((k, i) for i in items if (k := _rec_key(i)) is not None and _anchor(i)), key=lambda x: x[0])
    for i in items:
        k = _rec_key(i)
        i["rec_dup"] = bool(k is not None and counts.get(k, 0) > 1)
        if k is None:
            i["neighbors"] = None
            continue
        before = [(n, a) for n, a in anchors if n < k]
        after = [(n, a) for n, a in anchors if n > k]
        i["neighbors"] = {
            "prev": {"rec": before[-1][1]["rec"], "date": _anchor(before[-1][1])} if before else None,
            "next": {"rec": after[0][1]["rec"], "date": _anchor(after[0][1])} if after else None,
        }


# ── 에이전트 입력 ─────────────────────────────────────────────────────────

def find_item(idx: dict, key: str) -> dict:
    hits = [i for i in idx["items"] if key in (i.get("stem"), i.get("source"))]
    if not hits:
        raise SystemExit(f"색인에 없음: {key} (stem 또는 원본 파일명)")
    return hits[0]


def set_agent(item: dict, **fields) -> None:
    a = item.setdefault("agent", {})
    for k, v in fields.items():
        if k not in AGENT_FIELDS:
            raise ValueError(f"모르는 칸: {k}")
        if v is None:
            continue
        if k == "date_precision" and v not in PRECISIONS:
            raise SystemExit(f"정밀도는 {', '.join(PRECISIONS)} 중 하나")
        if k == "date_confidence" and v not in CONFIDENCES:
            raise SystemExit(f"확신도는 {', '.join(CONFIDENCES)} 중 하나")
        if k == "attendee_type" and v not in ATTENDEE_TYPES:
            raise SystemExit(f"참석자 유형은 {', '.join(ATTENDEE_TYPES)} 중 하나")
        if k == "date" and v and not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", v):
            raise SystemExit("날짜는 YYYY-MM-DD, YYYY-MM, YYYY 중 하나")
        a[k] = v
    # 확신도가 high가 아니면 일 단위 날짜를 쓰지 않는다(틀린 날짜를 단정하지 않게 정밀도를 내린다)
    if a.get("date_precision") == "day" and a.get("date_confidence") != "high":
        a["date_precision"] = "month"


def proposed(item: dict) -> str:
    a = item.get("agent") or {}
    if a.get("proposed_name"):
        return a["proposed_name"]
    return format_name(a.get("date"), a.get("date_precision"), a.get("topic"), item.get("rec"))


# ── index.md ─────────────────────────────────────────────────────────────

def _ts(sec) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _cell(v) -> str:
    return str(v if v not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def render_md(idx: dict) -> str:
    rows = sorted(idx["items"], key=lambda i: (_rec_key(i) is None, _rec_key(i) or 0, i["source"]))
    lines = [
        "# 녹취 백로그 색인",
        "",
        f"갱신 {idx.get('updated', '-')} · {len(rows)}건. 날짜 단서 원문은 index.json에만 있다.",
        "날짜 표기: 모르는 자리는 x(일 단위 = 확신도 high일 때만). 승인: `rec2md index approve <폴더> <stem>`.",
        "",
        "| 원본 | 길이(말소리) | 회의 구간 | 날짜(정밀도·확신도) | 근거 | 주제 | 참석자 | 한 줄 요약 | 제안 이름 | 승인 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i in rows:
        a = i.get("agent") or {}
        b = i.get("bounds") or {}
        bounds = f"{_ts(b.get('start'))}〜{_ts(b.get('end'))} ({b.get('confidence')})" if b else "-"
        date = f"{a['date']} ({a.get('date_precision') or '-'}·{a.get('date_confidence') or '-'})" if a.get("date") else "-"
        flags = "".join(f" [{f}]" for f in ("stale", "missing", "rec_dup") if i.get(f))
        ev = a.get("date_evidence")
        ev = "; ".join(ev) if isinstance(ev, list) else ev
        name = proposed(i) if (a.get("topic") or a.get("date")) else "-"
        lines.append("| " + " | ".join(_cell(v) for v in (
            f"{i['source']}{flags}", f"{_ts(i.get('duration'))} ({_ts(i.get('speech_sec'))})", bounds, date, ev,
            a.get("topic"), a.get("attendee_type"), a.get("summary"), name, "☑" if i.get("approved") else "☐",
        )) + " |")
    return "\n".join(lines) + "\n"


def write_md(out: Path, idx: dict) -> Path:
    p = out / INDEX_MD
    p.write_text(render_md(idx), encoding="utf-8")
    return p


# ── 이름 바꾸기 ───────────────────────────────────────────────────────────

def outputs_of(out: Path, stem: str, stems: set[str], sources: set[str] = frozenset()) -> list[Path]:
    """`<stem>.`으로 시작하는 출력 파일. 더 긴 다른 stem(`회의.v1`)의 파일은 그쪽 것으로 본다.
    전사 출력이 원본 옆에 있을 수 있으므로(`rec2md <파일>`의 기본) 원본 미디어와 미디어 확장자 파일은 뺀다."""
    longer = [s for s in stems if s != stem and s.startswith(stem + ".")]
    res = []
    for p in out.iterdir():
        if not (p.is_file() and p.name.startswith(stem + ".")) or any(p.name.startswith(s + ".") for s in longer):
            continue
        if p.name in sources or p.suffix.lower() in MEDIA_EXTS:
            continue
        res.append(p)
    return sorted(res)


def plan_renames(out: Path, idx: dict) -> list[dict]:
    """승인된 항목의 이름 바꾸기 계획. 문제가 있으면 그 항목은 skip 사유를 단다."""
    items = [i for i in idx["items"] if not i.get("missing")]
    stems = {i["stem"] for i in items}
    sources = {i["source"] for i in idx["items"]}
    existing = {p.name.casefold() for p in out.iterdir()}
    plans, taken = [], {}
    for i in items:
        if not i.get("approved"):
            continue
        new = proposed(i)
        p = {"source": i["source"], "old": i["stem"], "new": new, "files": [], "skip": None}
        clean = sanitize(new)
        if new != clean or not new:
            p["skip"] = f"파일명에 쓸 수 없는 문자·공백(제안: {clean})"
        elif len(new.encode()) > MAX_STEM_BYTES:
            p["skip"] = f"이름이 너무 김({len(new.encode())}바이트 > {MAX_STEM_BYTES})"
        elif new == i["stem"]:
            p["skip"] = "이미 그 이름"
        elif new.casefold() in taken:
            p["skip"] = f"같은 이름 제안이 겹침({taken[new.casefold()]})"
        elif i.get("stale"):
            p["skip"] = "원본 지문이 바뀜(stale) — 다시 확인 필요"
        if p["skip"] is None:
            taken[new.casefold()] = i["source"]
            p["files"] = [f.name for f in outputs_of(out, i["stem"], stems, sources)]
            targets = [new + f[len(i["stem"]):] for f in p["files"]]
            clash = [t for t in targets if t.casefold() in existing]
            if clash:
                p["skip"] = f"이미 있는 파일: {clash[0]}"
            elif not p["files"]:
                p["skip"] = "출력 파일 없음"
        plans.append(p)
    return plans


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def apply_renames(out: Path, idx: dict, plans: list[dict], copy_media: Path | None = None,
                  media_dirs: list[Path] = ()) -> list[dict]:
    """계획대로 출력 파일 이름을 바꾼다. 항목 하나 안에서 실패하면 그 항목만 되돌린다.
    copy_media를 주면 원본 미디어의 사본을 새 이름으로 그 폴더에 만든다(원본은 읽기만)."""
    state_p = out / STATE_FILE
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.is_file() else {}
    log_p = out / RENAMES
    log = json.loads(log_p.read_text(encoding="utf-8")) if log_p.is_file() else []
    done = []
    for p in plans:
        if p["skip"]:
            continue
        item = find_item(idx, p["source"])
        media_copy = None
        if copy_media is not None:
            src = media_path(p["source"], state, list(media_dirs))
            if src is None:
                p["skip"] = "원본 미디어를 찾을 수 없음(--media-dir)"
                continue
            if _same_dir(src.parent, copy_media):
                p["skip"] = "사본 폴더가 원본 폴더와 같음 — 원본 옆에는 쓰지 않는다"
                continue
            dst = copy_media / (p["new"] + src.suffix)
            if dst.exists():
                p["skip"] = f"사본 대상이 이미 있음: {dst.name}"
                continue
        moved: list[tuple[Path, Path]] = []
        try:
            for name in p["files"]:
                a, b = out / name, out / (p["new"] + name[len(p["old"]):])
                if b.exists():
                    raise FileExistsError(b.name)
                os.rename(a, b)
                moved.append((a, b))
            if copy_media is not None:
                copy_media.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                if _sha1(src) != _sha1(dst):
                    dst.unlink()
                    raise OSError("사본 검증 실패(sha1 다름)")
                media_copy = str(dst)
        except OSError as e:
            for a, b in reversed(moved):
                os.rename(b, a)
            p["skip"] = f"실패, 되돌림: {type(e).__name__}: {e}"
            continue
        item["renamed_from"] = item.get("renamed_from") or p["old"]
        item["stem"] = p["new"]
        rec = state.get(p["source"])
        if isinstance(rec, dict):
            rec["outputs"] = p["new"]
        entry = {"time": dt.datetime.now().isoformat(timespec="seconds"), "source": p["source"],
                 "old": p["old"], "new": p["new"], "files": p["files"], "media_copy": media_copy}
        log.append(entry)
        done.append(entry)
        save_json(log_p, log)  # 항목마다 남겨 중간에 끊겨도 되돌릴 수 있게
    if done:
        save_json(out / INDEX_JSON, idx)
        if state:
            save_json(state_p, state)
    return done


def undo_renames(out: Path, idx: dict, n: int | None = None) -> list[dict]:
    """기록의 뒤에서부터 n건(기본 전부) 되돌린다. 미디어 사본은 지우지 않고 경로만 알린다."""
    log_p = out / RENAMES
    log = json.loads(log_p.read_text(encoding="utf-8")) if log_p.is_file() else []
    take = list(log) if n is None else log[-n:] if n else []
    undone = []
    for e in reversed(take):
        pairs = [(out / (e["new"] + f[len(e["old"]):]), out / f) for f in e["files"]]
        if any(not b.exists() for b, _ in pairs) or any(a.exists() for _, a in pairs):
            e["undo_skip"] = "파일이 기록과 다름(이미 되돌렸거나 다시 바뀜)"
            continue
        for b, a in pairs:
            os.rename(b, a)
        for i in idx["items"]:
            if i["source"] == e["source"]:
                i["stem"] = e["old"]
                if i.get("renamed_from") == e["old"]:
                    i.pop("renamed_from")
        log.remove(e)
        undone.append(e)
        state_p = out / STATE_FILE
        if state_p.is_file():
            state = json.loads(state_p.read_text(encoding="utf-8"))
            if isinstance(state.get(e["source"]), dict) and state[e["source"]].pop("outputs", None):
                save_json(state_p, state)
    save_json(log_p, log)
    if undone:
        save_json(out / INDEX_JSON, idx)
    return undone


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(FP_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
