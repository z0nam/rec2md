# 작업 분배 (2026-10-30)

설계는 `docs/features-202610.md`, 회의록 스키마는 `docs/minutes-schema.md`. 조정 세션은 `rec2md-dev-main`(Claude)이다.

## 공통 규칙 (전원)
- **퍼블릭 레포.** 녹취, 전사 결과, 실제 녹취 파일명, 인명, 목소리 임베딩, 회사 양식 원본 파일은 커밋하지 않는다. 테스트는 합성 데이터만 쓴다(macOS `say -v Yuna` 등 합성 음성, 가짜 이름 "홍길동"류). 커밋 전 `git diff --cached` 확인.
- 실데이터 실험은 `samples/`, `local/`(gitignore)에서만 한다. 결과 수치는 PR 본문과 `docs/PLAN.md` 표에 **익명화해서** 적는다(유형·길이·수치만).
- 브랜치 → PR. main 직접 push 금지, **머지는 하지 않는다**(사용자가 지시함). PR을 만들면 `pr-watch <URL>`을 백그라운드로 실행하고, 지적은 전역 규칙대로 처리한다.
- PR 본문 끝 서명: `작성: 조남운 · <에이전트>/<세션이름> · <날짜>`
- 파일 소유: 아래 "주 파일" 밖을 고쳐야 하면 먼저 조정 세션에 알린다. `cli.py`는 서브커맨드 진입점만 있다. 새 명령은 `src/rec2md/cmd_<이름>.py`의 `main(argv)`로 만든다(`voices`, `render`, `series`는 예약됨).
- 새 의존성은 `pyproject.toml`의 optional extra로 넣는다(`voices`, `office`, `export` 등). 기본 설치를 무겁게 하지 않는다.
- 보고: Claude 일꾼은 `SendMessage(to="rec2md-dev-main")`, agy는 담당 GitHub 이슈의 코멘트로 보고한다. 시작, 막힘, PR 생성 때 보고한다.

## 담당

| ID | 세션 | 범위 | 브랜치 | 주 파일 |
|---|---|---|---|---|
| A | rec2md-worker-1 | 화자 목소리 DB, 자동 태깅, 평가 | `feat/voice-db` | `diarize.py`(임베딩 반환), `voices.py`, `cmd_voices.py`, `scripts/eval_voices.py` |
| B | rec2md-worker-2 | 맥락 보정: 시리즈 용어집, initial_prompt 실험, low_conf 보존, 보정 후보 | `feat/context-correction` | `engines/local_mlx.py`, `segments.py`(low_conf), `series.py`, `cmd_series.py`, `scripts/eval_terms.py` |
| C1 | rec2md-worker-3 | 회의록 렌더 코어: 양식 시스템, md·html, `rec2md render`, html → pdf·png | `feat/minutes-render` | `render/`(패키지로 분리 가능), `templates/`, `cmd_render.py`, `export.py` |
| C2 | rec2md-worker-4 | docx·hwpx 렌더러(`kr-standard`, `ji` 양식), docx·hwpx → pdf·png | `feat/office-render` | `render/docx.py`, `render/hwpx.py` |
| T | rec2md-worker-agy-1 | 테스트 기반: pytest, 합성 음성 픽스처, GitHub Actions(리눅스, mlx·pyannote 없이 도는 단위 테스트) | `chore/tests-ci` | `tests/`, `.github/workflows/`, `pyproject.toml`(dev 그룹) |
| K | rec2md-worker-agy-2 | 클로바식 녹취록 내보내기: segments.json → md·txt·html (클로바 txt 배치) | `feat/transcript-export` | `transcript_export.py`, `tests/test_transcript_export.py` |

### 의존 관계와 인터페이스
- C1과 C2는 `rec2md.minutes.Minutes`만 입력으로 받는다. 렌더러 시그니처는 `render_<fmt>(m: Minutes, template: Template, out: Path) -> Path`로 맞춘다. `Template`(양식 로더)은 C1이 먼저 만든다. C2는 C1의 PR이 열리기 전에는 `template.toml`을 직접 읽어 진행하고, 나중에 맞춘다.
- K는 함수만 만든다: `export_transcript(t: Transcript, fmt: str, out: Path, names: dict[str, str] | None) -> Path`. CLI 연결(`rec2md render --kind transcript`)은 C1이 한다.
- A의 `meta.speaker_names`를 K와 C1은 이름 표시에 쓴다. 키가 없으면 `화자N`.
- B의 `low_conf`는 segments.json의 선택 필드다. 없어도 다른 모듈은 동작해야 한다.
- T는 다른 PR이 머지되면 그 모듈의 테스트를 넓힌다. 처음에는 기존 모듈(`clean`, `clova_txt`, `diarize.speech_regions`, `_smooth`, `render`, `minutes`)부터 시작한다.

### 완료 기준
- A: 합성 화자 3명으로 enroll → match 테스트 통과. 실데이터 leave-one-out 결과(오태깅률, 미태깅률, 임계값)를 PR에 익명으로 보고.
- B: 같은 시리즈의 실제 녹취로 고유명사 적중률 보정 전후 비교(익명). initial_prompt를 기본으로 켤지에 대한 권고.
- C1: 합성 회의록 JSON → md·html·pdf·png 골든 테스트. `ji` 양식 md·html.
- C2: 합성 JSON → `kr-standard`, `ji` docx·hwpx 생성. hwpx는 rhwp 렌더 또는 한글에서 열림 확인(스크린샷은 로컬에만). `ji` hwpx 양식 원본은 `~/.config/rec2md/templates/ji/`에서 읽는다(레포에 넣지 않음).
- T: `uv run pytest`가 맥과 리눅스 CI에서 통과. 합성 픽스처를 만드는 스크립트를 커밋한다(생성물 wav는 작으면 커밋해도 된다).
- K: 클로바 txt 배치와 같은 모양의 txt를 만들고, `clova_txt` 파서로 다시 읽었을 때 왕복이 맞는다.
