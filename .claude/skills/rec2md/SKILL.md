---
name: rec2md
description: 녹음·영상 파일(회의, 전화 녹음, 강연, 인터뷰)을 화자 구분 녹취록 Markdown과 요약으로 바꾼다. "이 녹취 풀어줘", "녹취록 만들어줘", "회의록 정리", "전화 녹음 받아쓰기", "transcribe this recording", mp3·m4a·wav·mp4·mov 파일을 주며 내용을 글로 달라고 할 때, 녹취로 정해진 양식(국내 기관 공통, 제주연구원 등)의 회의록을 md·html·hwpx·docx·pdf로 만들 때 사용. 셸 명령 `rec2md`(PATH)로 전사하고, 정리·요약은 에이전트가 한다.
---

# rec2md — 녹음/영상 → 녹취록 Markdown

## 1. 전사 (CLI)

```sh
rec2md <파일...> [--speakers N] [--no-diarize] [--out <폴더>] [--language ko]
```

- 입력: ffmpeg가 읽는 오디오·영상 전부(영상은 소리만 씀).
- 출력(원본 옆, `--out`으로 변경): `<이름>.segments.json`(구간·화자·텍스트), `<이름>.raw.md`(타임스탬프 + `화자1`/`화자2` 라벨).
- 로컬 처리(Apple Silicon, mlx-whisper + pyannote). 외부 전송 없음. 대략 실시간의 수 배 속도라 1시간 녹음이면 수 분〜십여 분 걸린다 → 긴 파일은 백그라운드로 돌린다.
- 화자 수를 알면 `--speakers N`(전화 통화는 보통 2). 화자분리 설정(HF_TOKEN)이 없으면 오류 메시지대로 하거나 `--no-diarize`.
- 이 머신이 Apple Silicon이 아니면 local 엔진이 동작하지 않는다(원격·클라우드 엔진은 개발 중).

## 2. 정리 (에이전트)

`<이름>.raw.md`를 읽고 같은 폴더에 `<이름>.녹취록.md`를 쓴다.

1. **화자 이름**: 대화 속 호칭·자기소개·맥락으로 `화자N` → 실명/역할 추정. 확신이 없으면 추정 근거와 함께 사용자에게 확인. 녹음한 본인은 보통 가장 많이 말하는 쪽이 아님에 주의.
2. **정리**: 의미는 바꾸지 말고 군더더기("어", "그", 반복)만 걷어 문단화. 명백한 STT 오인식(고유명사·기관명·전문용어)은 맥락으로 고치되, 확신 없는 고침은 `[?]` 표시. 타임스탬프는 문단 앞에 유지.
   - `<이름>.corrections.json`이 있으면(전사 때 `--series`·`--context`를 준 경우) **고유명사 교정은 그 후보 구간만** 한다. 후보 밖은 명백한 경우가 아니면 손대지 않는다(전체를 다시 쓰면 멀쩡한 말까지 바뀐다).
     - 후보마다 `text`(원문)·`suggest`(용어집 쪽 제안)·`reasons`(오인식 매핑 / 자모거리 / 낮은 확률)·`context`(그 발화 전체)를 보고 판단한다. 근거는 용어집 항목, 같은 회의의 다른 발언, 안건 자료 중 하나 이상이어야 한다.
     - 고치면 그 후보의 `fix`에 고친 표기를, 근거를 `reasons`에 한 줄 더 적는다. 확신이 없으면 `fix`를 `"<표기>[?]"`로 두고 녹취록에도 `[?]`를 붙인다. 고치지 않을 후보는 `fix`를 `""`로 둔다.
     - 확정된 교정 중 다음 회차에도 나올 말은 사용자 확인 후 `rec2md series add <시리즈> --fix "오인식 => 정답"`으로 용어집에 쌓는다.
     - 시리즈 맥락: `rec2md series init <이름> [--from-clova <txt>]`, `series add`, `series show --prompt`, 이미 전사한 결과에서 후보만 다시 뽑기 `series candidates <이름> <segments.json>`.
3. **회의 구간**: `segments.json`의 `meta.meeting_bounds`(`start`·`end`·`confidence`·`evidence`)를 본다. 녹음기를 늦게 켜거나 안 꺼서 붙은 잡담·통화 후보다. `confidence`가 `none`이 아니면 근거 구간의 raw.md를 읽어 확인하고, 맞으면 요약은 그 구간만으로 쓰고 녹취록에는 범위 밖임을 표시한다(지우지 않는다). 애매하면 사용자에게 시각과 근거를 보여 주고 묻는다. 다시 보려면 `rec2md bounds <segments.json>`.
4. **요약**(맨 위): 개요 / 주요 논의 / 결정사항 / 할 일(담당·기한이 나오면 함께). 회의 구간을 정했으면 그 안의 내용만 쓴다.

파일명이 `REC00012` 처럼 무의미하면 내용으로 `YYYYMMDD_주제` 제목을 제안만 하고, 실제 이름 변경은 사용자 확인 후.

## 3. 회의록 (에이전트가 JSON → `rec2md render`)

"회의록 만들어줘", "○○ 양식으로", "hwp로 줘"처럼 **정해진 양식의 회의록**을 원하면 쓴다. 에이전트는 내용만 구조화하고, 양식·서식은 `rec2md render`가 맡는다(형식마다 직접 쓰지 않는다).

1. **근거**: 2단계의 `<이름>.녹취록.md`(없으면 `<이름>.raw.md`)를 읽는다. 화자 이름이 아직 `화자N`이면 2단계 1번을 먼저 한다.
2. **`<이름>.minutes.json` 쓰기** (원본 옆. 스키마 정본은 레포 `docs/minutes-schema.md`):
   ```json
   {"title": "회의명", "date": "2026-01-15", "time": "14:00-15:00", "place": "",
    "agenda": ["안건"], "attendees": [{"name": "홍길동", "org": "소속", "role": "직위"}],
    "author": "작성자", "owner": "연구자·주관자", "summary": ["개요 한 줄"],
    "sections": [{"heading": "소제목", "items": [{"text": "소항목", "ts": 125.0,
                  "children": [{"text": "내용"}]}]}],
    "decisions": ["결정사항"], "actions": [{"task": "할 일", "owner": "담당", "due": "기한"}],
    "next_meeting": "", "attachments": [], "source": {"recording": "<원본 파일명>", "range": [0, 3600]},
    "version": 1}
   ```
   - **지어내지 않는다.** 녹취에 없는 장소·직위·기한은 빈 문자열로 두고, 양식에 꼭 필요한 칸(작성자, 연구자 등)은 사용자에게 묻는다.
   - `sections`는 논의 주제별로 묶는다. 3단(소제목 → 소항목 → 내용)까지만. 문장은 회의록 관례대로 짧은 음슴체("~함", "~하기로 함")로 쓴다.
   - `ts`에는 근거 발언의 시각(초)을 raw.md에서 옮겨 둔다. 사람이 검증할 때 쓴다(기본 출력에는 안 보임).
   - `decisions`는 **명시적으로 합의된 것만**. 논의만 된 것은 `sections`에 둔다. `actions`의 담당·기한도 녹취에 나온 것만.
   - `source.range`는 2단계에서 확정한 회의 구간을 쓴다. 따로 정하지 않았으면 `meta.meeting_bounds`의 `[start, end]`가 기본값이다(`confidence`가 `none`이면 `[0, 길이]`).
   - `summary`는 3〜5줄. `agenda`가 녹취에 없으면 회의명이나 주제로 채우고 사용자에게 알린다.
3. **렌더**:
   ```sh
   rec2md render --list-templates                     # 쓸 수 있는 양식
   rec2md render <이름>.minutes.json --template <양식> --format md,html [--export pdf,png]
   rec2md render <이름>.minutes.json --template ji --format hwpx,docx    # 회사 원본이 있으면 그것을 채우고, 없으면 내장 골격
   ```
   - 양식: `kr-standard`(국내 기관 공통, 기본), `ji`(제주연구원 회의록 표). 사용자 양식은 `~/.config/rec2md/templates/<이름>/`(`template.toml`, `form.hwpx` 등). 회사 hwpx·docx 원본은 거기에만 둔다.
   - 출력은 JSON 옆 `<이름>.md`, `.html`, `.docx`, `.hwpx`, `.pdf`, `.p1.png`…(`--out`으로 폴더 변경). png는 쪽마다 한 장. pdf·png는 html이 있으면 html에서 만들고, html 없이 docx·hwpx를 같이 내보내면 `<이름>.docx.pdf`, `<이름>.hwpx.p1.png`처럼 형식별로 나뉜다.
   - 필요 패키지가 없다는 오류가 나면 안내대로 extra를 설치한다(`render`: md·html, `export`: pdf·png, `office`: docx·hwpx).
4. **확인**: `--export png`로 만든 첫 쪽 이미지를 직접 보고 칸 누락·넘침을 확인한 뒤 사용자에게 파일 경로와 함께 "녹취에 없어 비워 둔 칸"을 알린다. 사용자가 고친 내용은 JSON에 반영하고 다시 렌더한다(출력 파일을 직접 고치지 않는다).

클로바노트식 녹취록(`이름 00:00` + 발화)이 필요하면 JSON 없이 바로: `rec2md render <이름>.segments.json --kind transcript --format txt,md,html [--names 화자1=홍길동 ...]`. 이 설치본에 아직 없어 "녹취록 내보내기(rec2md.transcript_export)가 아직 없음"으로 끝나면, 2단계처럼 `<이름>.raw.md`를 읽어 같은 배치(제목 / 일시 ・ 길이 / 참석자 줄 / `이름 00:00` + 발화)로 직접 쓴다.

## 주의
- 녹취 내용은 개인정보다. 공개 저장소·외부 서비스에 올리지 않는다. `<이름>.minutes.json`과 렌더 결과도 같다.
