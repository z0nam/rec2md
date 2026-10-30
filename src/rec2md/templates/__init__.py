"""회의록 양식(template) 로더.

양식 하나는 폴더 하나다: `template.toml`(필드 → 위치 매핑, 회의내용 서식) + 형식별 원본
(`page.md.j2`, `page.html.j2`, `form.docx`, `form.hwpx`). 찾는 순서는
사용자 폴더 `~/.config/rec2md/templates/<이름>/` → 내장 `rec2md/templates/<이름>/` → 내장 `_base/`.
`template.toml`은 먼저 찾은 것 하나만 쓰고(병합하지 않음), 형식별 원본은 파일마다 위 순서로 찾는다.
그래서 내장 `ji`의 필드 매핑을 그대로 쓰면서 회사 hwpx 원본만 사용자 폴더에 둘 수 있다.

렌더러(md, html, docx, hwpx)는 값을 직접 계산하지 않고 `Template.values()`, `Template.content_lines()`를
쓴다. 그래야 형식마다 날짜·참석자·회의내용 표기가 같다. 스키마 설명은 docs/templates.md.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..minutes import Minutes

BUILTIN_DIR = Path(__file__).parent
BASE = "_base"


def user_dir() -> Path:
    """사용자 양식 폴더. 테스트·다른 머신은 REC2MD_TEMPLATES로 바꾼다."""
    env = os.environ.get("REC2MD_TEMPLATES")
    return Path(env).expanduser() if env else Path.home() / ".config" / "rec2md" / "templates"


# 필드 value가 가리킬 수 있는 키. 값은 항상 줄 목록(list[str])이다
VALUE_KEYS = ("title", "datetime", "date", "time", "place", "owner", "author", "attendees", "agenda",
              "summary", "content", "decisions", "actions", "next_meeting", "attachments")

# 형식별 기본 원본 파일 이름 (template.toml의 [files]로 바꿀 수 있음)
DEFAULT_FILES = {"md": "page.md.j2", "html": "page.html.j2", "docx": "form.docx", "hwpx": "form.hwpx"}


def norm(s: str) -> str:
    """양식 칸 텍스트 비교용: 공백 제거 ("참 석 자" == "참석자")."""
    return re.sub(r"\s+", "", s)


@dataclass
class Field:
    label: str            # 양식에 찍히는 칸 이름
    value: str            # VALUE_KEYS 중 하나
    row: int = 0          # 같은 row의 필드는 표 한 줄에 나란히 (html·docx·hwpx 표 배치)
    anchor: str = ""      # 원본 양식(form.*)에서 찾을 칸 텍스트. 기본은 label. 비교는 norm()으로
    where: str = "right"  # 값 칸이 anchor 칸의 오른쪽(right)인지 아래(below)인지
    block: bool = False   # 긴 본문(회의내용 등): md는 표 밖 절로, html은 한 줄 전체로

    def __post_init__(self):
        if self.value not in VALUE_KEYS:
            raise ValueError(f"알 수 없는 필드 값 '{self.value}' (가능: {', '.join(VALUE_KEYS)})")
        if self.where not in ("right", "below"):
            raise ValueError(f"where는 right|below: {self.where}")
        self.anchor = self.anchor or self.label


@dataclass
class ContentStyle:
    """회의내용(sections) 3단 표기. {n}은 1단 번호."""
    heading: str = "{n}. "
    item: str = "▶ "
    child: str = "- "
    # 회의내용 뒤에 이어 붙일 블록 (decisions, actions, next_meeting). 번호는 이어진다
    tail: list[str] = field(default_factory=list)
    tail_headings: dict[str, str] = field(default_factory=lambda: {
        "decisions": "결정사항", "actions": "향후 할 일", "next_meeting": "다음 회의"})
    summary: str = ""     # 비어 있지 않으면 회의내용 맨 앞에 이 제목으로 개요(summary)를 둔다
    show_ts: bool = False  # Item.ts를 [mm:ss]로 붙일지


@dataclass
class Line:
    """회의내용 한 줄. level 0 = 소제목, 1 = 소항목, 2 이상 = 내용."""
    level: int
    text: str             # 접두 없는 본문
    prefix: str = ""      # 양식 접두 ("1. ", "▶ ", "- ")
    ts: float | None = None

    def __str__(self) -> str:
        return self.prefix + self.text


@dataclass
class Template:
    name: str
    dirs: list[Path]                  # 원본 파일을 찾을 폴더, 우선순위 순
    title: str = "회의록"             # 문서 머리 제목. {title}을 쓰면 Minutes.title로 바뀐다
    description: str = ""
    fields: list[Field] = field(default_factory=list)
    content: ContentStyle = field(default_factory=ContentStyle)
    files: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FILES))
    date_style: str = "iso"           # iso: 2026-01-15 / ko: 2026년 1월 15일
    time_sep: str = "-"               # "14:00-15:00"의 구분자를 이걸로 바꿔 표시 (예: " – ")
    attendee_style: str = "org"       # org: 소속 줄 + "- 이름, 이름" / flat: "이름(소속 직위), …"
    attendee_role: bool = False       # 이름 뒤에 직위 표시
    toml_dir: Path | None = None      # template.toml이 있는 폴더 (dirs 중 하나)
    data: dict = field(default_factory=dict)  # template.toml 원문. 형식별 절([docx], [hwpx] …)은 렌더러가 직접 읽는다

    # ── 파일 ──────────────────────────────────────────────
    @property
    def dir(self) -> Path:
        """template.toml이 있는 폴더."""
        return self.toml_dir or self.dirs[0]

    def file(self, fmt_or_name: str) -> Path | None:
        """형식(`hwpx`) 또는 파일 이름(`form.hwpx`)으로 원본 파일을 찾는다. 없으면 None."""
        name = fmt_or_name if "." in fmt_or_name else self.files.get(fmt_or_name)
        if not name:
            return None
        for d in self.dirs:
            if (d / name).is_file():
                return d / name
        return None

    # ── 값 ────────────────────────────────────────────────
    def doc_title(self, m: Minutes) -> str:
        return self.title.replace("{title}", m.title)

    def fmt_date(self, s: str) -> str:
        mt = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s.strip())
        if self.date_style == "ko" and mt:
            y, mo, d = (int(x) for x in mt.groups())
            return f"{y}년 {mo}월 {d}일"
        return s

    def fmt_time(self, s: str) -> str:
        return re.sub(r"\s*-\s*", self.time_sep, s) if self.time_sep != "-" else s

    def attendee_lines(self, m: Minutes) -> list[str]:
        def who(a) -> str:
            return f"{a.name} {a.role}" if self.attendee_role and a.role else a.name

        if self.attendee_style == "flat":
            out = []
            for a in m.attendees:
                extra = " ".join(x for x in (a.org, a.role if not self.attendee_role else "") if x)
                out.append(f"{who(a)}({extra})" if extra else who(a))
            return [", ".join(out)] if out else []
        groups: dict[str, list[str]] = {}
        for a in m.attendees:  # 처음 나온 소속 순서를 지킨다
            groups.setdefault(a.org, []).append(who(a))
        lines = []
        for org, names in groups.items():
            if org:
                lines += [org, "- " + ", ".join(names)]
            else:
                lines.append(", ".join(names))
        return lines

    def action_text(self, a) -> str:
        extra = ", ".join(x for x in (f"담당: {a.owner}" if a.owner else "", f"기한: {a.due}" if a.due else "") if x)
        return f"{a.task} ({extra})" if extra else a.task

    def values(self, m: Minutes) -> dict[str, list[str]]:
        """VALUE_KEYS 각각의 표시값(줄 목록). 빈 값은 빈 목록."""
        date, time = self.fmt_date(m.date), self.fmt_time(m.time)
        one = lambda s: [s] if s else []  # noqa: E731
        return {
            "title": one(m.title),
            "datetime": one(" ".join(x for x in (date, time) if x)),
            "date": one(date),
            "time": one(time),
            "place": one(m.place),
            "owner": one(m.owner),
            "author": one(m.author),
            "attendees": self.attendee_lines(m),
            "agenda": list(m.agenda) or one(m.title),  # 안건이 없으면 회의명을 안건으로
            "summary": list(m.summary),
            "content": [str(x) for x in self.content_lines(m)],
            "decisions": list(m.decisions),
            "actions": [self.action_text(a) for a in m.actions],
            "next_meeting": one(m.next_meeting),
            "attachments": list(m.attachments),
        }

    def content_lines(self, m: Minutes) -> list[Line]:
        """회의내용을 양식 3단 표기로 편 줄 목록. 4단 이하는 내용(child) 접두로 들여쓰기만 늘린다."""
        st, out, n = self.content, [], 0

        def ts(x) -> str:
            if not (st.show_ts and x is not None):
                return ""
            x = int(x)
            return f" [{x // 60:02d}:{x % 60:02d}]" if x < 3600 else f" [{x // 3600}:{x % 3600 // 60:02d}:{x % 60:02d}]"

        def heading(text: str):
            nonlocal n
            n += 1
            out.append(Line(0, text, st.heading.replace("{n}", str(n))))

        def items(xs, level: int):
            for it in xs:
                out.append(Line(level, it.text + ts(it.ts), st.item if level == 1 else st.child, it.ts))
                items(it.children, level + 1)

        if st.summary and m.summary:
            heading(st.summary)
            out += [Line(1, s, st.child) for s in m.summary]  # 개요는 소항목 자리에 내용 접두로
        for sec in m.sections:
            heading(sec.heading)
            items(sec.items, 1)
        for key in st.tail:
            vals = self.values_tail(m, key)
            if vals:
                heading(st.tail_headings.get(key, key))
                out += [Line(1, v, st.item) for v in vals]
        return out

    def values_tail(self, m: Minutes, key: str) -> list[str]:
        if key == "decisions":
            return list(m.decisions)
        if key == "actions":
            return [self.action_text(a) for a in m.actions]
        if key == "next_meeting":
            return [m.next_meeting] if m.next_meeting else []
        raise ValueError(f"content.tail에 쓸 수 없는 키: {key} (decisions, actions, next_meeting)")

    # ── 표 배치 ────────────────────────────────────────────
    def rows(self) -> list[list[Field]]:
        """row 번호로 묶은 필드 줄. row를 안 쓴 필드는 한 줄에 하나씩, 선언 순서대로."""
        out: list[list[Field]] = []
        seen: dict[int, list[Field]] = {}
        for f in self.fields:
            if f.row and f.row in seen:
                seen[f.row].append(f)
                continue
            out.append([f])
            if f.row:
                seen[f.row] = out[-1]
        return out

    def field_for_anchor(self, cell_text: str) -> Field | None:
        """원본 양식의 칸 텍스트가 어느 필드의 anchor인지 (docx·hwpx 채우기용)."""
        key = norm(cell_text)
        return next((f for f in self.fields if norm(f.anchor) == key), None)


def available() -> list[str]:
    names = {p.parent.name for p in BUILTIN_DIR.glob("*/template.toml")}
    if user_dir().is_dir():
        names |= {p.parent.name for p in user_dir().glob("*/template.toml")}
    return sorted(names - {BASE})


def load_template(name_or_path: str | Path) -> Template:
    """이름(`ji`) 또는 폴더 경로로 양식을 읽는다."""
    p = Path(name_or_path).expanduser()
    if p.is_dir() and (p / "template.toml").is_file():
        name, dirs = p.name, [p]
    else:
        name = str(name_or_path)
        dirs = [d for d in (user_dir() / name, BUILTIN_DIR / name) if (d / "template.toml").is_file()]
        if not dirs:
            raise FileNotFoundError(f"양식 '{name}'이 없음 (가능: {', '.join(available())}; "
                                    f"사용자 양식은 {user_dir()}/<이름>/template.toml)")
        # 사용자 폴더에 template.toml 없이 원본(form.hwpx 등)만 둔 경우도 찾는다
        if (user_dir() / name).is_dir() and user_dir() / name not in dirs:
            dirs.insert(0, user_dir() / name)
    toml_dir = next(d for d in dirs if (d / "template.toml").is_file())
    conf = tomllib.loads((toml_dir / "template.toml").read_text(encoding="utf-8"))
    return from_dict(name, conf, dirs + [BUILTIN_DIR / BASE], toml_dir)


# 형식별 렌더러 전용 절. 로더는 검사하지 않고 Template.data로 넘긴다
FORMAT_SECTIONS = {"md", "html", "docx", "hwpx"}


def from_dict(name: str, conf: dict, dirs: list[Path], toml_dir: Path | None = None) -> Template:
    raw, conf = conf, {k: v for k, v in conf.items() if k not in FORMAT_SECTIONS}
    known = {"name", "title", "description", "fields", "content", "files", "date_style", "time_sep",
             "attendee_style", "attendee_role"}
    if unknown := set(conf) - known:
        raise ValueError(f"template.toml에 모르는 키: {', '.join(sorted(unknown))}")
    content = conf.pop("content", {})
    tail_headings = ContentStyle().tail_headings | content.pop("tail_headings", {})
    return Template(
        name=conf.pop("name", name),
        dirs=dirs,
        toml_dir=toml_dir,
        fields=[Field(**f) for f in conf.pop("fields", [])],
        content=ContentStyle(**content, tail_headings=tail_headings),
        files=DEFAULT_FILES | conf.pop("files", {}),
        data=raw,
        **conf,
    )
