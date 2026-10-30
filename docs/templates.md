# 회의록 양식 (`template.toml`)

로더는 `src/rec2md/templates/__init__.py`(`load_template`, `Template`). `rec2md render --template <이름|폴더>`.

## 찾는 순서
1. 사용자 폴더 `~/.config/rec2md/templates/<이름>/` (환경변수 `REC2MD_TEMPLATES`로 바꿀 수 있음)
2. 내장 `src/rec2md/templates/<이름>/`
3. 내장 `src/rec2md/templates/_base/` (기본 `page.md.j2`, `page.html.j2`)

`template.toml`은 1·2 중 먼저 찾은 **하나만** 쓴다(병합 안 함). 형식별 원본 파일은 **파일마다** 1→2→3 순서로 찾는다.
그래서 내장 `ji` 매핑을 그대로 쓰면서 회사 원본 `form.hwpx`만 `~/.config/rec2md/templates/ji/`에 두면 된다(회사 양식 원본은 레포에 넣지 않는다).

## 스키마
```toml
name = "ji"                 # 생략하면 폴더 이름
title = "회의록"            # 문서 머리 제목. "{title}"은 Minutes.title로 바뀜
description = "…"
date_style = "ko"           # iso(2026-01-15) | ko(2026년 1월 15일)
time_sep = " – "            # "14:00-15:00"의 "-"를 바꿔 표시
attendee_style = "org"      # org: 소속 줄 + "- 이름, 이름" | flat: "이름(소속 직위), …"
attendee_role = false       # 이름 뒤 직위

[files]                     # 형식별 원본 이름 (아래는 기본값)
md = "page.md.j2"
html = "page.html.j2"
docx = "form.docx"
hwpx = "form.hwpx"

[[fields]]                  # 선언 순서 = 표 순서
label = "참석자"            # 칸 이름
value = "attendees"         # 값 키 (아래 표)
row = 0                     # 같은 row(>0)끼리 한 줄에 나란히
anchor = "참 석 자"          # 원본 양식에서 찾을 칸 텍스트 (기본 label, 공백 무시 비교)
where = "right"             # 값 칸 위치: anchor의 right | below
block = false               # 긴 본문: md는 표 밖 절, html은 한 줄 전체

[content]                   # 회의내용(sections) 3단 표기
heading = "{n}. "           # 1단 접두, {n} 번호
item = "▶ "                 # 2단 접두
child = "- "                # 3단 이하 접두
summary = "회의 개요"        # 비어 있지 않으면 개요(summary)를 첫 소제목으로
tail = ["decisions", "actions", "next_meeting"]   # 회의내용 뒤에 번호를 이어 붙일 블록
show_ts = false             # 근거 발언 시각 [mm:ss] 표시
[content.tail_headings]
decisions = "결정사항"

[docx]                      # 형식별 렌더러 전용 절. 로더는 검사하지 않고 Template.data로 넘긴다
[hwpx]
```

### 값 키 (`value`)
`title`, `datetime`(date+time), `date`, `time`, `place`, `owner`, `author`, `attendees`, `agenda`(없으면 title), `summary`, `content`(회의내용 3단 + tail), `decisions`, `actions`(`할 일 (담당: …, 기한: …)`), `next_meeting`, `attachments`.

## 렌더러가 쓰는 API
- `t.values(m) -> dict[값 키, list[str]]` — 모든 값은 줄 목록. 형식마다 표기가 같도록 값 계산은 여기서만 한다.
- `t.content_lines(m) -> list[Line]` — `Line(level, text, prefix, ts)`, `str(line)`은 `prefix + text`. level 0 소제목, 1 소항목, 2+ 내용.
- `t.rows() -> list[list[Field]]` — 표 배치. `t.field_for_anchor(칸 텍스트) -> Field | None` — 원본 양식 채우기용.
- `t.file("hwpx")` 또는 `t.file("form.hwpx")` — 원본 경로 또는 None. `t.dir` — template.toml 폴더. `t.data` — toml 원문.
- 렌더러 시그니처: `rec2md.render.<fmt>.render_<fmt>(m: Minutes, template: Template, out: Path) -> Path`.
  pdf 변환은 같은 모듈의 `to_pdf(src: Path, out: Path) -> Path`(선택), 쪽별 png는 `to_png(src: Path, out_dir: Path) -> list[Path]`(선택).
- **내보내기 규약**: `rec2md.export`는 html만 직접 바꾸고(Chrome → WeasyPrint), 다른 형식은 `rec2md.render.<형식>`의 `to_pdf`/`to_png`를 찾아 부르기만 한다. 함수나 외부 도구(soffice, rhwp, Chrome)가 없으면 **무엇을 설치하면 되는지** 담은 `rec2md.export.ExportError`로 끝낸다.

## docx·hwpx (extra `office`는 docx만 필요)

### hwpx (`rec2md.render.hwpx`, 표준 라이브러리만)
- **원본 `form.hwpx`가 있으면** 표 칸을 `anchor` 글자(공백 무시)로 찾아 `where`(right|below) 칸을 채운다. 값 칸의 기존 문단이 단계별 문단 모양의 견본이다: 단계 i(회의내용 `1.`/`▶`/`-`, 참석자 `소속`/`- 이름`)는 i번째 문단(없으면 마지막)의 문단·글자 모양을 따른다. 본문 어디든 `{{title}}`, `{{doc_title}}`, `{{place}}` 같은 값 키 자리표시자도 바뀐다.
- **원본이 없으면** `[[fields]]` 배치로 표 양식을 만든다(뼈대는 `_base/skeleton.hwpx`, python-hwpx의 빈 문서, Apache-2.0 — `_base/skeleton-hwpx.NOTICE`). 내장 `kr-standard`와 회사 원본이 없는 `ji`가 이 경로다.
- section XML은 다시 직렬화하지 않고 원문 문자열의 구간만 바꾼다(한글이 재직렬화한 문서를 변조로 의심함). 새 문단에는 줄 배치 캐시를 넣지 않는다 — 한글이 열 때 계산한다.
- `[hwpx]` 절: `required = false`면 원본에 없는 칸을 건너뛴다(기본은 오류). `form_required = true`면 원본이 없을 때 표를 만들지 않고 오류.
- **회사 양식 만들기**: 실제로 채워진 회의록 hwpx에서 값만 비워 `~/.config/rec2md/templates/<이름>/form.hwpx`로 둔다(레포에 넣지 않음). 미리보기 그림·글도 지운다.
  ```python
  from rec2md.render.hwpx import blank_form
  blank_form("채워진_회의록.hwpx",
             {"회의일시": "right", "연구자": "right", "작성자": "right", "참석자": "right",
              "회의장소": "right", "회의안건": "right", "내용": "below"},
             Path.home() / ".config/rec2md/templates/ji/form.hwpx",
             markers={"참석자": ["제주연구원", "-"], "내용": ["1.", "▶", "-"]})  # 단계별 견본 문단을 고르는 머리글자
  ```
- pdf·png: rhwp(`export-svg`, PATH 또는 `REC2MD_RHWP`) → rsvg-convert(librsvg). png는 rsvg-convert가 없으면 macOS `qlmanage`.
  rhwp(v0.7.17)는 줄 배치 캐시 없는 문단이 여럿인 TOP 정렬 칸을 한 줄에 겹쳐 그려서, 내보내기용 사본에서만 그런 칸을 가운데 정렬로 바꾼다. 또 캐시가 없으면 행을 늘리지 않으므로 채울 때 칸·행·표 높이를 어림해 늘리되 표가 한 쪽을 넘지 않게 한다 — **아주 긴 회의내용은 hwpx 미리보기(pdf·png)에서 잘릴 수 있다.** 한글에서는 다음 쪽으로 이어진다. 긴 회의록의 pdf는 docx나 html에서 만드는 편이 낫다.

### docx (`rec2md.render.docx`, python-docx · docxtpl)
- **원본 `form.docx`가 있으면** docxtpl(Jinja2)로 채운다. 변수: `doc_title`, 값 키마다 한 덩어리 글(`{{ place }}`, 여러 줄은 줄바꿈 유지), `lines.<값 키>`(줄 목록), `content`(회의내용 줄: `x.level`, `x.prefix`, `x.text`, `x.ts`), `m`(Minutes 원본 dict).
- **원본이 없으면** python-docx로 `[[fields]]` 배치 표를 만든다(A4, 맑은 고딕, 라벨 칸 음영, 회의내용은 단계마다 들여쓰기).
- pdf: LibreOffice(`soffice --headless --convert-to pdf`, `REC2MD_SOFFICE`로 경로 지정 가능). png는 그 pdf를 쪽마다.
