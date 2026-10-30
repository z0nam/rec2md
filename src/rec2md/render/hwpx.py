"""hwpx 회의록 렌더러. 양식 hwpx의 표 칸을 라벨 글자로 찾아 옆(또는 아래) 칸을 채운다.

hwpx는 zip 안의 XML이다. 외부 의존성 없이 표준 라이브러리만 쓴다.

- 칸 찾기는 template.toml `[[fields]]`의 `anchor`(기본 label)와 `where`(right|below)를 쓴다.
  비교는 공백을 뺀다(`참 석 자` = `참석자`). 문서에서 처음 나오는 칸만 쓴다.
- 값은 `Template.values()`·`content_lines()`에서 받는다(형식 간 표기 통일).
- 채울 칸의 기존 문단이 단계별 문단 모양의 견본이다. 단계 i는 i번째 문단(없으면 마지막)의
  문단·글자 모양을 복제한다. 그래서 양식의 값 칸에는 단계 수만큼 빈 문단을 남겨 둔다
  (`blank_form()`이 채워진 회의록에서 그렇게 만들어 준다).
- 양식 본문 어디든 `{{title}}`, `{{doc_title}}` 같은 자리표시자를 쓰면 한 줄 값으로 바뀐다.
- 양식 원본(form.hwpx)이 없으면 `[[fields]]` 배치로 표 양식을 만들어 채운다(`build_form`).
- `[hwpx] required = false`이면 양식에 없는 칸은 건너뛴다(기본은 오류).

XML은 DOM으로 다시 직렬화하지 않고 **원문 문자열의 구간만 바꾼다.** 한글은 다시 직렬화한
section을 변조로 의심한다(hwp-agent의 실측). 새 문단에는 줄 배치 캐시(linesegarray)를 넣지 않는다 —
한글이 열 때 계산한다. 칸이 넘치면 칸·행·표 높이를 어림해 늘리고 뒤 문단의 줄 위치를 민다(rhwp 미리보기용).
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..minutes import Minutes

Lines = list[tuple[int, str]]

SECTION_RE = re.compile(r"Contents/section\d+\.xml")
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
TAG_RE = re.compile(r"<(/?)hp:(tbl|tc|subList|p)\b[^>]*?(/?)>")
T_RE = re.compile(r"<hp:t(?:\s[^>]*)?(?:/>|>(.*?)</hp:t>)", re.S)
RUN_RE = re.compile(r"<hp:run\b([^>]*?)(?:/>|>(.*?)</hp:run>)", re.S)
LINESEG_RE = re.compile(r"<hp:linesegarray>.*?</hp:linesegarray>", re.S)


def render_hwpx(m: Minutes, template, out: Path) -> Path:
    cfg = template.data.get("hwpx", {})
    vals = template.values(m)
    ph = {k: "\n".join(v) for k, v in vals.items()} | {"doc_title": template.doc_title(m)}
    form = template.file("hwpx")
    if form is not None:
        return fill_form(form, _cells(m, template, vals), out, ph, required=cfg.get("required", True))
    if cfg.get("form_required", False):
        raise FileNotFoundError(
            f"양식 '{template.name}'에 hwpx 원본({template.files.get('hwpx')})이 없음 — "
            f"~/.config/rec2md/templates/{template.name}/ 에 두세요")
    # 원본이 없으면 칸 배치로 표 양식을 만들어 채운다
    with tempfile.TemporaryDirectory() as tmp:
        form = build_form(template, Path(tmp) / "form.hwpx")
        return fill_form(form, _cells(m, template, vals, built=True), out, ph)


def _cells(m: Minutes, template, vals: dict[str, list[str]], built: bool = False) -> dict[str, tuple[str, Lines]]:
    """라벨 → (방향, 줄). `built`면 build_form이 만든 표라 label 오른쪽이 값 칸이다(anchor·where는 원본 양식용)."""
    cells: dict[str, tuple[str, Lines]] = {}
    for f in template.fields:
        if f.value == "content":
            lines = [(x.level, str(x)) for x in template.content_lines(m)]
        elif f.value == "attendees":  # 소속 줄(0) 아래 "- 이름" 줄(1)
            lines = [(1 if s.startswith("- ") else 0, s) for s in vals["attendees"]]
        else:
            lines = [(0, s) for s in vals[f.value]]
        cells[f.label if built else f.anchor] = ("right" if built else f.where, lines)
    return cells


# ── 채우기 ────────────────────────────────────────────────────────────

def fill_form(form: Path, cells: dict[str, tuple[str, Lines]], out: Path,
              placeholders: dict[str, str] | None = None, required: bool = True) -> Path:
    """`cells`: 라벨 → (방향 "right"|"below", 줄 목록). `required`면 찾지 못한 라벨은 KeyError."""
    entries = _read_zip(form)
    metrics = _metrics(entries.get("Contents/header.xml", b""))
    found: set[str] = set()
    texts: list[str] = []
    for name, data in entries.items():
        if SECTION_RE.fullmatch(name):
            xml = data.decode("utf-8")
            # 자리표시자는 양식에서 먼저 바꾼다 — 채운 값에 든 `{{…}}` 글자는 그대로 둬야 한다
            if placeholders:
                xml = _replace_placeholders(xml, placeholders)
            xml, hit = _fill_section(xml, cells, metrics)
            entries[name] = xml.encode("utf-8")
            found |= hit
            texts.append(_plain(xml))
    if required and (missing := set(cells) - found):
        raise KeyError(f"양식에서 라벨을 찾지 못함: {', '.join(sorted(missing))}")
    _write_zip(form, Path(out), entries, preview="\n".join(texts))
    return Path(out)


def blank_form(src: Path, labels: dict[str, str], out: Path, markers: dict[str, list[str]] | None = None) -> Path:
    """채워진 hwpx에서 값 칸을 비운 양식을 만든다(사용자 양식 만들기용).

    `labels`: 라벨 → 방향. `markers`: 라벨 → 단계별 머리기호(예: ["1.", "▶", "-"]).
    머리기호가 있으면 그 칸에서 단계마다 그 기호로 시작하는 문단 하나를 견본으로 남기고 글은 비운다.
    """
    markers = markers or {}
    entries = _read_zip(src)
    for name, data in entries.items():
        if SECTION_RE.fullmatch(name):
            xml = data.decode("utf-8")
            root = _tree(xml)
            edits = []
            for key, tc in _targets(xml, root, labels).items():
                ps = _paras(tc)
                keep = _pick_protos(xml, ps, markers.get(key))
                edits.append((ps[0].start, ps[-1].end, "".join(_para_like(xml[p.start:p.end], "") for p in keep)))
            entries[name] = _apply(xml, edits).encode("utf-8")
    _write_zip(src, Path(out), entries, preview="")
    return Path(out)


@dataclass
class _Node:
    kind: str
    start: int            # 여는 태그 시작
    open_end: int         # 여는 태그 끝
    end: int = -1         # 닫는 태그 끝
    close_start: int = -1
    parent: _Node | None = None
    children: list[_Node] = field(default_factory=list)


def _tree(xml: str) -> _Node:
    """tbl·tc·subList·p만 뽑은 가벼운 트리 (원문 위치를 그대로 들고 있다)."""
    root = cur = _Node("root", 0, 0, len(xml), len(xml))
    for mt in TAG_RE.finditer(xml):
        closing, kind, selfclose = mt.group(1), mt.group(2), mt.group(3)
        if closing:
            if cur.kind != kind:
                raise ValueError(f"hwpx XML 구조를 읽지 못함: </hp:{kind}> 위치 {mt.start()}")
            cur.close_start, cur.end = mt.start(), mt.end()
            cur = cur.parent
            continue
        node = _Node(kind, mt.start(), mt.end(), parent=cur)
        cur.children.append(node)
        if selfclose:
            node.close_start = node.end = mt.end()
        else:
            cur = node
    return root


def _walk(node: _Node, kind: str):
    for c in node.children:
        if c.kind == kind:
            yield c
        yield from _walk(c, kind)


def _attrs(tag: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', tag))


def _own(xml: str, tc: _Node, tag: str) -> re.Match | None:
    """칸 자신의 `<hp:{tag} …/>` (안쪽 표의 것은 제외 — subList 뒤에 온다)."""
    sub = next((c for c in tc.children if c.kind == "subList"), None)
    lo = sub.end if sub else tc.open_end
    return re.compile(rf"<hp:{tag}\b[^>]*/>").search(xml, lo, tc.close_start)


def _addr(xml: str, tc: _Node) -> tuple[int, int, int, int]:
    a = _attrs(_own(xml, tc, "cellAddr").group(0))
    sp = _own(xml, tc, "cellSpan")
    s = _attrs(sp.group(0)) if sp else {}
    return int(a["rowAddr"]), int(a["colAddr"]), int(s.get("rowSpan", 1)), int(s.get("colSpan", 1))


def _text(xml: str, lo: int, hi: int) -> str:
    return "".join(html.unescape(re.sub(r"<[^>]+>", "", t or "")) for t in T_RE.findall(xml, lo, hi))


def _cell_text(xml: str, tc: _Node) -> str:
    sub = next((c for c in tc.children if c.kind == "subList"), None)
    return _text(xml, sub.open_end, sub.close_start) if sub else ""


def _paras(tc: _Node) -> list[_Node]:
    sub = next(c for c in tc.children if c.kind == "subList")
    return [c for c in sub.children if c.kind == "p"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _targets(xml: str, root: _Node, labels: dict[str, str]) -> dict[str, _Node]:
    """라벨 → 값 칸. 표마다 자기 칸만 보고(안쪽 표는 따로), 처음 나온 라벨만 쓴다."""
    want = {_norm(k): k for k in labels}
    out: dict[str, _Node] = {}
    for tbl in _walk(root, "tbl"):
        tcs = [c for c in tbl.children if c.kind == "tc"]
        for tc in tcs:
            key = want.get(_norm(_cell_text(xml, tc)))
            if not key or key in out:
                continue
            r, c, rs, cs = _addr(xml, tc)
            for other in tcs:
                r2, c2, _, _ = _addr(xml, other)
                if (labels[key] == "right" and (r2, c2) == (r, c + cs)) or \
                   (labels[key] == "below" and (r2, c2) == (r + rs, c)):
                    out[key] = other
                    break
    return out


def _para_like(proto: str, text: str) -> str:
    """견본 문단(원문 문자열)의 문단·글자 모양으로 글 한 줄짜리 새 문단을 만든다."""
    open_tag = re.match(r"<hp:p\b[^>]*>", proto).group(0)
    runs = RUN_RE.findall(proto)
    # 글자가 있는 첫 run의 글자 모양을 쓴다 (빈 run이 앞에 오는 양식이 많다)
    attrs = next((a for a, body in runs if _text(body or "", 0, len(body or "")).strip()),
                 runs[0][0] if runs else ' charPrIDRef="0"')
    t = f"<hp:t>{html.escape(text, quote=False)}</hp:t>" if text else "<hp:t/>"
    return f"{open_tag}<hp:run{attrs}>{t}</hp:run></hp:p>"


def _pick_protos(xml: str, ps: list[_Node], marks: list[str] | None) -> list[_Node]:
    if not marks:
        return ps[:1]
    keep: list[_Node] = []
    for mk in marks:
        pat = re.compile(r"\d+\." if mk == "1." else re.escape(mk))
        hits = [p for p in ps if pat.match(_text(xml, p.start, p.end).strip())]
        if not hits:
            keep.append(keep[-1] if keep else ps[0])
            continue
        # 같은 단계 문단 중 가장 흔한 문단 모양을 견본으로 (한두 줄만 들여쓰기를 바꾼 경우가 있다)
        pp = lambda p: _attrs(xml[p.start:p.open_end]).get("paraPrIDRef")  # noqa: E731
        common, _ = Counter(pp(p) for p in hits).most_common(1)[0]
        keep.append(next(p for p in hits if pp(p) == common))
    return keep


def _fill_section(xml: str, cells: dict[str, tuple[str, Lines]], metrics) -> tuple[str, set[str]]:
    root = _tree(xml)
    targets = _targets(xml, root, {k: v[0] for k, v in cells.items()})
    edits = []
    grow: dict[tuple[int, int], tuple[_Node, _Node, int]] = {}  # (표 위치, 행) → (표, 칸, 필요 높이)
    for key, tc in targets.items():
        ps = _paras(tc)
        protos = [xml[p.start:p.end] for p in ps]
        new = [_para_like(protos[min(level, len(protos) - 1)], text) for level, text in cells[key][1] or [(0, "")]]
        edits.append((ps[0].start, ps[-1].end, "".join(new)))
        tbl = tc.parent
        need = _need_height(xml, tbl, tc, new, metrics)
        row = _addr(xml, tc)[0]
        if need > grow.get((tbl.start, row), (None, None, 0))[2]:
            grow[(tbl.start, row)] = (tbl, tc, need)
    edits += _grow_edits(xml, root, grow)
    return _apply(xml, edits), set(targets)


def _apply(xml: str, edits: list[tuple[int, int, str]]) -> str:
    for lo, hi, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        xml = xml[:lo] + rep + xml[hi:]
    return xml


# ── 칸 높이 ───────────────────────────────────────────────────────────

def _metrics(header: bytes) -> tuple[dict[str, int], dict[str, tuple[str, int, int]]]:
    """header.xml에서 글자 크기(charPr id → height)와 줄 간격(paraPr id → (종류, 값, 왼쪽 여백))."""
    xml = header.decode("utf-8", "replace")
    chars = {i: int(h) for i, h in re.findall(r'<hh:charPr id="(\d+)" height="(\d+)"', xml)}
    paras = {}
    for i, body in re.findall(r'<hh:paraPr id="(\d+)"(.*?)</hh:paraPr>', xml, re.S):
        ls = re.search(r'<hh:lineSpacing type="(\w+)" value="(\d+)"', body)
        left = re.search(r'<hc:left value="(-?\d+)"', body)
        paras[i] = (ls.group(1) if ls else "PERCENT", int(ls.group(2)) if ls else 160,
                    int(left.group(1)) if left else 0)
    return chars, paras


def _text_width(text: str, size: int) -> float:
    # 한글·한자·전각은 글자 크기만큼, 나머지는 대략 절반 폭
    return sum(size if ord(ch) > 0x2E80 else size * 0.5 for ch in text)


def _need_height(xml: str, tbl: _Node, tc: _Node, paras: list[str], metrics) -> int:
    """새 문단들을 담는 데 필요한 칸 높이(HWPUNIT) 어림."""
    chars, pprs = metrics
    sz = _attrs(_own(xml, tc, "cellSz").group(0))
    mg = _own(xml, tc, "cellMargin")
    mg = _attrs(mg.group(0)) if mg else {}
    inm = re.compile(r"<hp:inMargin\b[^>]*/>").search(xml, tbl.open_end)
    inm = _attrs(inm.group(0)) if inm else {}
    pick = lambda k: max(int(mg.get(k, 0)), int(inm.get(k, 0)))  # noqa: E731
    width = int(sz["width"]) - pick("left") - pick("right")
    need = pick("top") + pick("bottom") + 200
    for p in paras:
        size = chars.get(_attrs(RUN_RE.search(p).group(1)).get("charPrIDRef", "0"), 1000)
        kind, val, left = pprs.get(_attrs(re.match(r"<hp:p\b[^>]*>", p).group(0)).get("paraPrIDRef"),
                                   ("PERCENT", 160, 0))
        line = val if kind == "FIXED" else size * max(val, 100) // 100
        n = max(1, -(-int(_text_width(_text(p, 0, len(p)), size)) // max(width - left, 1000)))
        need += n * line
    return need


def _grow_edits(xml: str, root: _Node, grow) -> list[tuple[int, int, str]]:
    """모자란 행을 늘린다: 그 행에 걸친 칸(병합 칸 포함)·표 높이를 같이 늘리고, 뒤 문단의 줄 위치를 민다.

    한글은 열 때 행 높이를 스스로 다시 잡으므로 이것은 rhwp 같은 렌더러를 위한 어림이다. 표가 쪽 본문
    높이를 넘으면 rhwp가 표를 위로 끌어올리므로, 표 전체가 한 쪽 안에 들 만큼까지만 늘린다
    (아주 긴 회의내용은 rhwp 미리보기에서 잘릴 수 있다 — 한글에서는 다음 쪽으로 이어진다).
    """
    body = _body_height(xml)
    by_tbl: dict[int, list] = {}
    for tbl, tc, need in grow.values():
        by_tbl.setdefault(tbl.start, []).append((tbl, tc, need))
    add_tc: dict[int, tuple[_Node, int]] = {}
    add_tbl: dict[int, tuple[_Node, int]] = {}
    pushed = 0  # 앞 표들이 늘어난 만큼 뒤 표가 내려간다
    for key in sorted(by_tbl):
        rows = by_tbl[key]
        tbl = rows[0][0]
        room = body - 1000 - pushed - _top_of(xml, root, tbl) - int(_attrs(re.compile(r"<hp:sz\b[^>]*/>").search(
            xml, tbl.open_end).group(0))["height"])
        for _, tc, need in sorted(rows, key=lambda x: _addr(xml, x[1])[0]):
            cur = int(_attrs(_own(xml, tc, "cellSz").group(0))["height"])
            delta = min(need - cur, max(room, 0))
            if delta <= 0:
                continue
            room -= delta
            row = _addr(xml, tc)[0]
            for other in (c for c in tbl.children if c.kind == "tc"):
                r, _, rs, _ = _addr(xml, other)
                if r <= row < r + rs:
                    add_tc[other.start] = (other, add_tc.get(other.start, (other, 0))[1] + delta)
            add_tbl[tbl.start] = (tbl, add_tbl.get(tbl.start, (tbl, 0))[1] + delta)
            pushed += delta
    edits = []
    for tc, delta in add_tc.values():
        mt = _own(xml, tc, "cellSz")
        edits.append((mt.start(), mt.end(), _bump_height(mt.group(0), delta)))
    for tbl, delta in add_tbl.values():
        mt = re.compile(r"<hp:sz\b[^>]*/>").search(xml, tbl.open_end)
        edits.append((mt.start(), mt.end(), _bump_height(mt.group(0), delta)))
    edits += _shift_linesegs(xml, root, add_tbl)
    return edits


def _body_height(xml: str) -> int:
    """쪽 본문 높이 (용지 높이 - 위·아래 여백 - 머리말·꼬리말)."""
    pg = re.search(r"<hp:pagePr\b[^>]*>", xml)
    mg = re.search(r"<hp:margin\b[^>]*/>", xml[pg.end():]) if pg else None
    if not (pg and mg):
        return 84188 - 2 * 5668
    a, m = _attrs(pg.group(0)), _attrs(mg.group(0))
    h = int(a.get("width" if a.get("landscape") == "NARROWLY" else "height", 84188))
    return h - sum(int(m.get(k, 0)) for k in ("top", "bottom", "header", "footer"))


def _top_of(xml: str, root: _Node, tbl: _Node) -> int:
    """표를 담은 최상위 문단의 줄 위치(캐시가 없으면 앞 최상위 문단 수로 어림)."""
    top = tbl
    while top.parent is not None and top.parent is not root:
        top = top.parent
    ls = _own_lineseg(xml, top) if top.kind == "p" else None
    if ls and (vp := re.search(r'\bvertpos="(\d+)"', ls.group(0))):
        return int(vp.group(1))
    tops = [c for c in root.children if c.kind == "p"]
    return 2400 * tops.index(top) if top in tops else 0


def _own_lineseg(xml: str, p: _Node) -> re.Match | None:
    """문단 자신의 linesegarray (안에 든 표·칸 문단의 것은 제외 — 문단 끝에 온다)."""
    inner = [n.end for n in _walk(p, "p")] + [n.end for n in _walk(p, "tbl")]
    return LINESEG_RE.search(xml, max(inner + [p.open_end]), p.close_start)


def _shift_linesegs(xml: str, root: _Node, add_tbl: dict[int, tuple[_Node, int]]) -> list[tuple[int, int, str]]:
    """늘어난 표를 담은 최상위 문단의 줄 높이를 늘리고, 그 뒤 최상위 문단들의 줄 위치(vertpos)를 민다.

    캐시를 지우지 않고 고치는 것은 (1) 개요 문단이 캐시 없이는 본문으로 떨어지고(hwp-agent 실측)
    (2) rhwp가 캐시 없는 표 문단이 이어지면 표 위치를 잘못 잡기 때문이다. 한글은 열 때 다시 계산한다.
    """
    tops = [c for c in root.children if c.kind == "p"]
    holder_of = {}
    for tbl, delta in add_tbl.values():
        top = tbl
        while top.parent is not None and top.parent is not root:
            top = top.parent
        if top.kind == "p":
            holder_of[top.start] = holder_of.get(top.start, 0) + delta
    edits, shift = [], 0
    for p in tops:
        ls = _own_lineseg(xml, p)
        grow = holder_of.get(p.start, 0)
        if ls and (shift or grow):
            edits.append((ls.start(), ls.end(), _shift_seg(ls.group(0), shift, grow)))
        shift += grow
    return edits


def _shift_seg(seg: str, shift: int, grow: int) -> str:
    def add(name: str, by: int, s: str) -> str:
        return re.sub(rf'\b{name}="(-?\d+)"', lambda mt: f'{name}="{int(mt.group(1)) + by}"', s)

    seg = add("vertpos", shift, seg) if shift else seg
    if grow:  # 표 하나를 담은 줄: 줄 높이·글 높이·기준선이 표 높이를 따른다
        for name in ("vertsize", "textheight", "baseline"):
            seg = add(name, grow, seg)
    return seg


def _bump_height(tag: str, delta: int) -> str:
    return re.sub(r'\bheight="(\d+)"', lambda mt: f'height="{int(mt.group(1)) + delta}"', tag, count=1)


# ── 자리표시자·zip ────────────────────────────────────────────────────

def _replace_placeholders(xml: str, ph: dict[str, str]) -> str:
    root = _tree(xml)
    edits = []
    for mt in PLACEHOLDER_RE.finditer(xml):
        if mt.group(1) not in ph:
            continue
        edits.append((mt.start(), mt.end(), html.escape(ph[mt.group(1)], quote=False)))
        # 글이 바뀐 문단의 줄 배치 캐시는 버린다
        p = min((p for p in _walk(root, "p") if p.start < mt.start() < p.end), key=lambda p: p.end - p.start,
                default=None)
        if p is not None:
            inner = [c for c in _walk(p, "p")]
            hi = min([c.start for c in inner if c.start > mt.start()] + [p.close_start])
            ls = LINESEG_RE.search(xml, mt.end(), hi)
            if ls and not any(e[0] == ls.start() for e in edits):
                edits.append((ls.start(), ls.end(), ""))
    return _apply(xml, edits)


def _plain(xml: str) -> str:
    return "\n".join(t for p in re.findall(r"<hp:p\b.*?</hp:p>", xml, re.S) if (t := _text(p, 0, len(p))))


def _read_zip(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def _write_zip(src: Path, out: Path, entries: dict[str, bytes], preview: str) -> None:
    """src의 항목 순서·압축 방식을 지켜 다시 묶는다(mimetype은 맨 앞, 무압축)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as z:
        for info in zin.infolist():
            data = entries[info.filename]
            # 미리보기에 양식 원본(채워진 회의록일 수 있음)이 남지 않게 새로 쓴다
            if info.filename == "Preview/PrvText.txt":
                data = preview[:1024].encode("utf-8")
            elif info.filename == "Preview/PrvImage.png":
                data = _blank_png()
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
            zi.external_attr = info.external_attr
            z.writestr(zi, data)


def _blank_png(w: int = 8, h: int = 11) -> bytes:
    """흰 png. 미리보기 그림 자리를 채운다(한글이 저장할 때 다시 만든다)."""
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    raw = b"".join(b"\x00" + b"\xff" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ── 양식 원본이 없을 때: template.toml 칸 배치로 표 양식을 만든다 ────────────────
# 뼈대는 python-hwpx의 빈 문서(templates/_base/skeleton.hwpx, Apache-2.0)다.
# 여기에 표 테두리·라벨 음영·굵은 글자·가운데 정렬·들여쓰기 모양을 덧붙인다.

SKELETON = "skeleton.hwpx"
TEXT_WIDTH = 42520         # 뼈대 A4 본문 폭 (59528 - 8504*2), HWPUNIT
LABEL_WIDTH = 7000
ROW_HEIGHT = 2400
BLOCK_HEIGHT = 12000
LEVEL_INDENT = 1200        # 단계마다 왼쪽 여백

# 뼈대 header.xml의 마지막 id 다음 번호를 쓴다
BF_LINE, BF_LABEL = 3, 4                 # borderFill: 실선 / 실선+음영
CP_BOLD, CP_TITLE = 7, 8                 # charPr: 굵게 / 제목(16pt 굵게)
PP_CENTER, PP_L1, PP_L2 = 20, 21, 22     # paraPr: 가운데 / 1·2단계 들여쓰기


def build_form(template, out: Path, skeleton: Path | None = None) -> Path:
    """template.toml의 `[[fields]]` 배치로 빈 표 양식 hwpx를 만든다.

    라벨 칸 오른쪽이 값 칸이다. 같은 row의 필드는 한 줄에 나란히, block 필드는 값 칸에 3단 견본 문단을 둔다.
    맨 위 제목은 `{{doc_title}}` 자리표시자라 채울 때 `Template.doc_title()`로 바뀐다.
    """
    skeleton = skeleton or template.file(SKELETON)
    if skeleton is None:
        raise FileNotFoundError(f"hwpx 뼈대({SKELETON})가 없음")
    entries = _read_zip(skeleton)
    entries["Contents/header.xml"] = _extend_header(entries["Contents/header.xml"].decode("utf-8")).encode("utf-8")
    entries["Contents/section0.xml"] = _form_section(entries["Contents/section0.xml"].decode("utf-8"),
                                                     template).encode("utf-8")
    _write_zip(skeleton, Path(out), entries, preview="")
    return Path(out)


ITEM_TAGS = {"borderFills": "borderFill", "charProperties": "charPr", "paraProperties": "paraPr"}


def _bump(xml: str, tag: str, add: str) -> str:
    """`<hh:{tag} itemCnt="n">…</hh:{tag}>` 끝에 항목을 붙이고 개수를 고친다."""
    m = re.search(rf'<hh:{tag} itemCnt="(\d+)">', xml)
    n = int(m.group(1)) + add.count(f"<hh:{ITEM_TAGS[tag]} ")
    xml = xml[:m.start()] + f'<hh:{tag} itemCnt="{n}">' + xml[m.end():]
    end = xml.index(f"</hh:{tag}>")
    return xml[:end] + add + xml[end:]


def _border(i: int, fill: str = "") -> str:
    side = 'type="SOLID" width="0.12 mm" color="#000000"'
    brush = f'<hc:fillBrush><hc:winBrush faceColor="{fill}" hatchColor="#000000" alpha="0"/></hc:fillBrush>' if fill else ""
    return (f'<hh:borderFill id="{i}" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
            '<hh:slash type="NONE" Crooked="0" isCounter="0"/><hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
            f'<hh:leftBorder {side}/><hh:rightBorder {side}/><hh:topBorder {side}/><hh:bottomBorder {side}/>'
            f'<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>{brush}</hh:borderFill>')


def _extend_header(xml: str) -> str:
    xml = _bump(xml, "borderFills", _border(BF_LINE) + _border(BF_LABEL, "#EDEDED"))
    base_cp = re.search(r'<hh:charPr id="0".*?</hh:charPr>', xml, re.S).group(0)

    def cp(i: int, height: int) -> str:
        s = re.sub(r'id="0"', f'id="{i}"', base_cp, count=1)
        s = re.sub(r'height="\d+"', f'height="{height}"', s, count=1)
        return s.replace("<hh:underline", "<hh:bold/><hh:underline", 1)

    xml = _bump(xml, "charProperties", cp(CP_BOLD, 1000) + cp(CP_TITLE, 1600))
    base_pp = re.search(r'<hh:paraPr id="0".*?</hh:paraPr>', xml, re.S).group(0)

    def pp(i: int, align: str = "JUSTIFY", left: int = 0) -> str:
        s = re.sub(r'id="0"', f'id="{i}"', base_pp, count=1)
        s = s.replace('horizontal="JUSTIFY"', f'horizontal="{align}"', 1)
        return s.replace('<hc:left value="0"', f'<hc:left value="{left}"')

    return _bump(xml, "paraProperties",
                 pp(PP_CENTER, "CENTER") + pp(PP_L1, left=LEVEL_INDENT) + pp(PP_L2, left=2 * LEVEL_INDENT))


def _xml_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _p(text: str = "", para: int = 0, char: int = 0) -> str:
    t = f"<hp:t>{_xml_text(text)}</hp:t>" if text else "<hp:t/>"
    return (f'<hp:p id="2147483648" paraPrIDRef="{para}" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
            f'<hp:run charPrIDRef="{char}">{t}</hp:run></hp:p>')


def _tc(row: int, col: int, colspan: int, width: int, height: int, paras: str, bf: int, align: str) -> str:
    return (f'<hp:tc name="" header="0" hasMargin="0" protect="0" editable="0" dirty="0" borderFillIDRef="{bf}">'
            f'<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="{align}" linkListIDRef="0" '
            'linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">'
            f'{paras}</hp:subList><hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
            f'<hp:cellSpan colSpan="{colspan}" rowSpan="1"/><hp:cellSz width="{width}" height="{height}"/>'
            '<hp:cellMargin left="510" right="510" top="141" bottom="141"/></hp:tc>')


def _form_table(template) -> str:
    rows = template.rows()
    pairs = max(len(r) for r in rows)
    value_w = (TEXT_WIDTH - pairs * LABEL_WIDTH) // pairs
    trs, total_h = [], 0
    for ri, row in enumerate(rows):
        h = BLOCK_HEIGHT if any(f.block for f in row) else ROW_HEIGHT
        total_h += h
        cells = []
        for fi, f in enumerate(row):
            col = 2 * fi
            span = 2 * pairs - col - 1 if fi == len(row) - 1 else 1  # 마지막 값 칸이 남은 폭을 차지
            width = TEXT_WIDTH - col // 2 * (LABEL_WIDTH + value_w) - LABEL_WIDTH if span > 1 else value_w
            cells.append(_tc(ri, col, 1, LABEL_WIDTH, h, _p(f.label, PP_CENTER, CP_BOLD), BF_LABEL, "CENTER"))
            protos = _p(para=0) + _p(para=PP_L1) + _p(para=PP_L2) if f.block or f.value == "attendees" else _p()
            cells.append(_tc(ri, col + 1, span, width, h, protos, BF_LINE, "TOP" if f.block else "CENTER"))
        trs.append("<hp:tr>" + "".join(cells) + "</hp:tr>")
    return (f'<hp:tbl id="1000000001" zOrder="0" numberingType="TABLE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" '
            f'lock="0" dropcapstyle="None" pageBreak="CELL" repeatHeader="0" rowCnt="{len(rows)}" colCnt="{2 * pairs}" '
            f'cellSpacing="0" borderFillIDRef="{BF_LINE}" noAdjust="0">'
            f'<hp:sz width="{TEXT_WIDTH}" widthRelTo="ABSOLUTE" height="{total_h}" heightRelTo="ABSOLUTE" protect="0"/>'
            '<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" '
            'vertRelTo="PARA" horzRelTo="PARA" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>'
            '<hp:outMargin left="0" right="0" top="0" bottom="0"/><hp:inMargin left="510" right="510" top="141" bottom="141"/>'
            + "".join(trs) + "</hp:tbl>")


def _form_section(xml: str, template) -> str:
    # 첫 문단(secPr)은 그대로 두고 그 뒤에 제목·표 문단을 붙인다. 뼈대의 줄 배치 캐시는 지운다
    xml = re.sub(r"<hp:linesegarray>.*?</hp:linesegarray>", "", xml, flags=re.S)
    table_p = ('<hp:p id="2147483648" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
               f'<hp:run charPrIDRef="0">{_form_table(template)}<hp:t/></hp:run></hp:p>')
    end = xml.rindex("</hs:sec>")
    return xml[:end] + _p("{{doc_title}}", PP_CENTER, CP_TITLE) + _p() + table_p + xml[end:]


# ── pdf·png (rhwp) ───────────────────────────────────────────────────
# rhwp(https://github.com/edwardkim/rhwp)로 쪽마다 svg를 만든 뒤 rsvg-convert(librsvg)로 pdf·png.
# rhwp(v0.7.17)는 줄 배치 캐시(linesegarray)가 없는 문단이 여럿인 칸을 제대로 못 그린다: TOP 정렬이면 한 줄에
# 겹쳐 그리고, 칸이 쪽을 넘으면 문단마다 한 쪽씩 쓴다. 내보내기용 사본에서만 그런 칸의 문단마다 줄 배치를
# 어림해 넣는다(`_layout_cache`). 결과 hwpx에는 넣지 않는다 — 한글은 캐시가 없으면 스스로 계산한다.
# 한 문단짜리 칸은 rhwp가 스스로 줄을 바꾸므로 건드리지 않는다(어림한 줄바꿈이 오히려 어긋난다).

RHWP_CANDIDATES = ["rhwp", str(Path.home() / ".local" / "bin" / "rhwp")]


def find_rhwp() -> str | None:
    import os

    if env := os.environ.get("REC2MD_RHWP"):
        return env
    return next((c for c in RHWP_CANDIDATES if shutil.which(c)), None)


def to_svg(src: Path, out_dir: Path) -> list[Path]:
    from ..export import ExportError

    rhwp = find_rhwp()
    if rhwp is None:
        raise ExportError("hwpx → pdf·png에 rhwp가 필요함: https://github.com/edwardkim/rhwp 를 받아 PATH에 두거나 "
                          "REC2MD_RHWP로 경로 지정. 없으면 한글에서 열어 'PDF로 저장'")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rec2md-rhwp-") as td:
        fixed = Path(td) / src.name
        entries = _read_zip(src)
        metrics = _metrics(entries.get("Contents/header.xml", b""))
        for name, data in entries.items():
            if SECTION_RE.fullmatch(name):
                entries[name] = _layout_cache(data.decode("utf-8"), metrics).encode("utf-8")
        _write_zip(src, fixed, entries, preview="")
        r = subprocess.run([rhwp, "export-svg", str(fixed), "-o", td, "--embed-fonts"],
                           capture_output=True, text=True, timeout=300)
        svgs = sorted(Path(td).glob("*.svg"), key=lambda p: [int(x) if x.isdigit() else x
                                                             for x in re.split(r"(\d+)", p.stem)])
        if r.returncode != 0 or not svgs:
            raise ExportError(f"rhwp 변환 실패: {(r.stderr or r.stdout).strip()[-500:]}")
        stem = src.name[: -len(src.suffix)]
        outs = []
        for i, f in enumerate(svgs, 1):
            outs.append(Path(shutil.move(f, out_dir / f"{stem}.p{i}.svg")))
        return outs


def _layout_cache(xml: str, metrics) -> str:
    """문단이 여럿인 칸에서 줄 배치 캐시가 없는 문단에 어림한 캐시를 넣는다(rhwp 미리보기용).

    줄바꿈 위치는 `_text_width`의 글자 폭 어림으로 정한다. 실제 글꼴 폭과 달라 줄 끝이 한두 글자
    어긋날 수 있지만, 문단이 겹치거나 쪽마다 한 문단씩 흩어지지는 않는다.
    """
    chars, pprs = metrics
    root = _tree(xml)
    edits = []
    for tc in _walk(root, "tc"):
        sub = next((c for c in tc.children if c.kind == "subList"), None)
        ps = [c for c in sub.children if c.kind == "p"] if sub else []
        if len(ps) < 2:
            continue
        sz = _attrs(_own(xml, tc, "cellSz").group(0))
        mg = _own(xml, tc, "cellMargin")
        mg = _attrs(mg.group(0)) if mg else {}
        width = int(sz["width"]) - int(mg.get("left", 510)) - int(mg.get("right", 510))
        y = 0
        for p in ps:
            own = _own_lineseg(xml, p)
            if own is not None:  # 원래 있던 캐시는 두고, 그 아래부터 이어 쌓는다
                segs = re.findall(r'vertpos="(\d+)" vertsize="(\d+)"[^>]*?spacing="(-?\d+)"', own.group(0))
                if segs:
                    v, h, sp = map(int, segs[-1])
                    y = v + h + sp
                continue
            if any(True for _ in _walk(p, "tbl")):
                continue
            body = xml[p.start:p.end]
            run = RUN_RE.search(body)
            size = chars.get(_attrs(run.group(1)).get("charPrIDRef", "0"), 1000) if run else 1000
            kind, val, left = pprs.get(_attrs(xml[p.start:p.open_end]).get("paraPrIDRef"), ("PERCENT", 160, 0))
            line = val if kind == "FIXED" else size * max(val, 100) // 100
            segs = []
            for pos in _wrap(_text(body, 0, len(body)), size, max(width - left, size)):
                segs.append(f'<hp:lineseg textpos="{pos}" vertpos="{y}" vertsize="{size}" textheight="{size}" '
                            f'baseline="{size * 85 // 100}" spacing="{line - size}" horzpos="{left}" '
                            f'horzsize="{max(width - left, size)}" flags="393216"/>')
                y += line
            edits.append((p.close_start, p.close_start, "<hp:linesegarray>" + "".join(segs) + "</hp:linesegarray>"))
    return _apply(xml, edits)


def _wrap(text: str, size: int, width: int) -> list[int]:
    """줄마다 시작 글자 위치. 넘치면 마지막 공백에서, 공백이 없으면 그 글자에서 끊는다."""
    starts, cur, last_space = [0], 0.0, -1
    for i, ch in enumerate(text):
        w = _text_width(ch, size)
        if cur + w > width and i > starts[-1]:
            cut = last_space + 1 if last_space >= starts[-1] else i
            starts.append(cut)
            cur = _text_width(text[cut:i + 1], size)
            last_space = -1
            continue
        cur += w
        if ch == " ":
            last_space = i
    return starts


def _rsvg() -> str | None:
    return shutil.which("rsvg-convert")


def to_pdf(src: Path, out: Path) -> Path:
    from ..export import ExportError

    if not _rsvg():
        raise ExportError("svg → pdf에 rsvg-convert(librsvg)가 필요함: brew install librsvg / apt install librsvg2-bin")
    with tempfile.TemporaryDirectory(prefix="rec2md-svg-") as td:
        svgs = to_svg(src, Path(td))
        subprocess.run([_rsvg(), "-f", "pdf", "-o", str(out), *map(str, svgs)], check=True, timeout=300)
    return out


def to_png(src: Path, out_dir: Path, width: int = 1600) -> list[Path]:
    from ..export import ExportError

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rec2md-svg-") as td:
        outs = []
        for svg in to_svg(src, Path(td)):
            png = out_dir / (svg.stem + ".png")
            if _rsvg():
                subprocess.run([_rsvg(), "-w", str(width), "-b", "white", "-o", str(png), str(svg)],
                               check=True, timeout=120)
            elif shutil.which("qlmanage"):  # macOS 기본 도구
                subprocess.run(["qlmanage", "-t", "-s", str(width), "-o", td, str(svg)],
                               capture_output=True, timeout=120)
                shutil.move(Path(td) / (svg.name + ".png"), png)
            else:
                raise ExportError("svg → png에 rsvg-convert(librsvg)가 필요함: brew install librsvg / "
                                  "apt install librsvg2-bin")
            outs.append(png)
        return outs
