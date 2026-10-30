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
