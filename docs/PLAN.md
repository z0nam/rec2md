# 녹취록 스킬 `rec2md` — 1단계 계획 (영상→슬라이드는 2단계)

## Context
- 현재 회의·전화 녹취를 네이버 클로바노트로 처리 중. 업로드, 다운로드, 복사·붙여넣기를 손으로 해야 해서 번거로움.
- 클로바노트 개인판은 API가 없어 자동화가 불가능함. 웍스판 Team 요금제(월 10.8만원)에만 비공개 API가 있음.
- 한국어 화자분리 녹취와 요약을 해주는 Claude Code 스킬·MCP 서버는 사실상 없음.
  - 가장 가까운 것은 openai/skills `transcribe`(gpt-4o-transcribe-diarize). 요청당 25MB 제한이 있고 미국 클라우드를 씀.
- 그래서 직접 만드는 작은 CLI와 스킬로 간다. 부품은 이미 성숙해 있고, 묶어주는 부분만 만들면 됨.
- 같은 녹취 모듈을 2단계(컨퍼런스 영상→슬라이드+노트)에서 재사용함.

## 실행 환경 (2026-10-16 결정)
- **개발 거점은 studio(Mac, `~/dev/rec2md`)** — ~/dev 프로젝트 지도·라우팅 규약이 있는 메인 작업공간이고, local 엔진(mlx)이 네이티브로 돈다. ffmpeg·uv·claude 설치돼 있음.
- **namun-ji(Windows)**: 녹음기(E:\REC_DIR)가 꽂히는 곳. GTX 1060 3GB라 로컬 STT 부적합. CLI를 `uv tool install`로 설치해 클라우드 엔진 호출·일괄 업로드용으로 씀. ffmpeg는 winget으로 설치 완료(9.0.2).
- **local 엔진**: studio에서 `mlx-whisper` large-v3(또는 turbo) + pyannote community-1(MPS). 외부 전송 없음 → 민감 녹취·백로그용.
- E:는 녹음기 마운트라 상시 접근 불가 → 개발용 샘플은 `samples/`(gitignore)에 복사해 둠.

## 접근
엔진을 플러그인처럼 갈아끼우는 Python CLI로 만들고, Claude가 정리와 요약을 맡는다.

```
rec2md <audio> [--engine rtzr|clova|local] [--call] [--speakers N] [--remote studio]
  1. ffmpeg로 정규화 (16kHz mono wav)
  2. 엔진 호출 → 공통 스키마 segments.json
     [{start, end, speaker, text}]
  3. raw transcript .md 작성 (타임스탬프 + 화자 라벨)
→ SKILL.md 지시로 Claude가 후처리:
     화자 이름 매핑(대화에서 추론하거나 사용자에게 확인), 군더더기 정리, 문단화
     → 녹취록.md + 요약(개요 / 논의사항 / 결정사항 / 할일)
```

### 엔진
| 엔진 | 용도 | 비고 |
|---|---|---|
| `rtzr` (기본 후보) | 일반 회의·전화 | 시간당 약 1,000원, 처음 10시간 무료, `domain=CALL`(전화), 화자분리, 4h/2GB |
| `clova` | 정확도 비교·대체 | CLOVA Speech 장문 비동기 API, NCP 계정 필요 |
| `local` | 민감 녹취 | Mac에서 mlx-whisper와 pyannote 사용. VAD(무음 구간 필터링)를 켜서 무음 구간에 엉뚱한 문장이 생기는 문제 방지 |

- `--remote studio`: Windows에서 `scp`로 파일을 studio에 보내고, `ssh studio rec2md --engine local`을 실행한 뒤 결과 json을 회수한다.
- **방침: 클라우드 우선, 로컬은 백업.** 기본은 클라우드 엔진(rtzr 또는 clova)으로 처리한다. 로컬(Mac)은 민감한 녹취이거나 클라우드가 실패·한도 초과일 때만 쓴다.
- rtzr과 clova 중 무엇을 기본으로 할지는 **파일럿으로 결정**한다. 같은 녹취 2~3건(회의 1, 전화 1)으로 비교하고, local도 백업 품질 확인용으로 함께 돌림.

### 일괄 처리 (기존 녹취 백로그)
- `E:\REC_DIR` 실측(2026-10-16): MP3 167개, 10.1GB, **총 188.5시간**, 전부 128kbps. 파일별 목록은 `local/backlog-durations.tsv`(gitignore — 파일명에 인명·회의명 포함).
  - 30초 미만 빈 파일 15개 → 건너뜀.
  - **4시간 초과 8개**(최장 360분) → RTZR 4h 제한 때문에 무음 지점 기준 분할 후 이어붙이기 필요(화자 라벨은 청크 간 불연속 — 정리 단계에서 Claude가 맞춤).
  - `YYYYMMDD_주제.MP3`로 이름 붙인 파일과 `REC000NN.MP3`가 섞여 있음. `REC000NN`은 녹음기 시계가 없어 수정일 1601년 → 날짜·주제는 내용에서 추론.
- `rec2md batch <dir> [--engine ...] [--dry-run]`
  - `--dry-run`: ffprobe로 총 재생시간과 엔진별 예상 비용 출력.
  - 처리한 파일은 상태 파일(`.rec2md-state.json`)에 기록해 건너뜀 → 중단 후 재개 가능.
  - 출력은 `<dir>/_transcripts/`에 모음.
- `REC000NN` 파일: 요약 기반으로 `YYYYMMDD?_주제` 제목을 제안하는 `index.md` 생성. 실제 파일명 변경은 사용자 확인 후.
- 비용: RTZR 기준 188시간 ≈ 18〜19만원(처음 10시간 무료). 백로그는 studio local로 밤새 무료 처리하고 새 녹취는 클라우드로 하는 분담도 선택지 — 파일럿 품질 비교 후 결정.

### 패키징 (기존 `gw` 패턴 재사용)
- uv 패키지, `[project.scripts] rec2md = "rec2md.cli:main"`, 설치는 `uv tool install --editable` (참고: namun-ji `ji-gw-ai/pyproject.toml`).
- 설정은 `~/.config/rec2md/.env`: `RTZR_CLIENT_ID/SECRET`, `CLOVA_INVOKE_URL/SECRET`, `HF_TOKEN`(pyannote), `REC2MD_REMOTE_HOST`(기본 `studio`).
- 스킬은 `.claude/skills/rec2md/SKILL.md`, `~/.claude/skills/rec2md`로 심볼릭 링크(양 머신).
  - description에 트리거 키워드(녹취, 녹취록, 회의록, 전화 녹음, 받아쓰기, transcribe, recording)를 넣어 이름과 무관하게 호출되게 함.
  - 이름 선정 이유: `transcribe`는 openai/skills의 동명 스킬·흔한 CLI 이름과 충돌. `rec2md`는 `hwp2pdf` 작명 패턴과 맞고 2단계 영상 기능(`rec2md talk`)까지 포괄. PyPI 미사용 확인(404), GitHub엔 무관한 개인 레포 1개(bulatovv/rec2md)뿐.
- 레포 구성: `src/rec2md/{cli,audio,render,remote,batch}.py`, `src/rec2md/engines/{base,rtzr,clova,local_mlx}.py`
- **레포는 퍼블릭**(github.com/z0nam/rec2md). 녹취·전사 결과·실제 파일명·인명은 커밋 금지 — `samples/`, `local/`, `*.segments.json`, `*.raw.md`, `*.녹취록.md`는 gitignore.
- 출력은 원본 옆에 `<이름>.segments.json`, `<이름>.raw.md`, `<이름>.녹취록.md`.

### 선행 작업
- ~~Windows: `winget install Gyan.FFmpeg`~~ 완료
- RTZR 개발자 가입 (무료 600분)
- Mac: `brew install ffmpeg`, `uv tool install` 후 mlx-whisper·pyannote 설치. Hugging Face 토큰과 pyannote 모델 약관 동의 필요.
- Windows에서 Mac으로 SSH 키 접속 확인

## 2단계 미리보기 (이번 범위 아님)
`talk2notes`:
1. 영상을 ffmpeg로 프레임 추출
2. OpenCV로 화면 검출, 원근 보정, 추적
3. pHash와 SSIM으로 슬라이드 구간 분리
4. 구간 내 프레임을 median 합성해 가림 제거
5. `rec2md` segments를 구간별로 배정
6. Claude vision이 슬라이드별로 요약
7. md / PDF / pptx(발표자 노트에 녹취)로 출력

1차 범위는 삼각대 촬영이고, 손으로 들고 찍은 영상은 이후로 미룬다. 무거운 처리는 Mac에서 함.

## 검증
1. 짧은 샘플(1~2분)로 엔진마다 `rec2md sample.m4a --engine X`를 실행해 segments.json 스키마가 맞고 화자 라벨이 붙는지 확인.
2. 실제 회의 녹취(30~60분)와 전화 녹취 각 1건으로 엔진 3개를 비교한다. 비교 항목은 인명·전문용어 오인식, 화자 혼동, 처리시간, 비용이고, 표로 정리해서 기본 엔진을 정함.
3. Claude Code에서 "이 녹취 풀어줘 <경로>"라고 하면 스킬이 트리거되어 녹취록.md와 요약까지 한 번에 나오는지 확인.
4. Windows에서 `--remote studio` 왕복이 동작하는지 확인.
