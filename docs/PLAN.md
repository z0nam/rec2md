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
- **namun-ji(Windows)**: 녹음기(E:\REC_DIR)가 꽂히는 곳. GTX 1060 3GB라 로컬 STT 부적합. CLI를 `uv tool install`로 설치해 녹음기 파일을 studio로 보내는(`--remote studio`) 입구로 씀. 클라우드 엔진도 여기서 직접 호출 가능. ffmpeg는 winget으로 설치 완료(9.0.2).
- **local 엔진**: studio에서 `mlx-whisper` large-v3(또는 turbo) + pyannote community-1(MPS). 외부 전송 없음, 추가 비용 없음 → **기본 엔진**.
- E:는 녹음기 마운트라 상시 접근 불가 → 개발용 샘플은 `samples/`(gitignore)에 복사해 둠.

## 접근
엔진을 플러그인처럼 갈아끼우는 Python CLI로 만들고, Claude가 정리와 요약을 맡는다.

```
rec2md <audio> [--engine local|rtzr|clova]  # 기본 local [--call] [--speakers N] [--remote studio]
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
| `local` (**기본**) | 백로그·일상 녹취 전부 | studio에서 mlx-whisper + pyannote. VAD(무음 구간 필터링)로 무음 구간 환각 방지. 비용·시간 제한 없음 |
| `rtzr` | 품질 기준점·보조 | 계정당 무료 10시간을 파일럿 비교에 사용. 이후 시간당 약 1,000원. `domain=CALL`(전화), 화자분리, 4h/2GB |
| `clova` | 품질 기준점·보조 | CLOVA Speech 장문 API(클로바노트와 같은 계열 엔진, 클로바노트 앱 한도와는 별개). NCP 가입·결제 필요, 단가는 콘솔 확인 |

- `--remote studio`: Windows에서 `scp`로 파일을 studio에 보내고, `ssh studio rec2md --engine local`을 실행한 뒤 결과 json을 회수한다.
- **방침: 로컬 우선, 클라우드는 기준점·보조** (2026-10-16 변경). 처음엔 클라우드 우선이었으나, studio가 로컬 엔진을 충분히 돌릴 수 있고 클라우드 무료 한도(RTZR 계정당 10시간)로는 188시간 백로그를 감당할 수 없어 뒤집음.
- 클라우드는 (1) 파일럿에서 로컬 품질을 재는 기준점, (2) 로컬이 크게 밀리는 유형(전화·잡음 현장 등)에 한해 선택 사용.
- 파일럿: 같은 녹취 2〜3건(회의 1, 전화 1)을 local과 rtzr(무료 한도 내), 가능하면 clova로 돌려 비교. 로컬 처리속도(실시간 대비 배수)도 실측.

### 일괄 처리 (기존 녹취 백로그)
- `E:\REC_DIR` 실측(2026-10-16): MP3 167개, 10.1GB, **총 188.5시간**, 전부 128kbps. 파일별 목록은 `local/backlog-durations.tsv`(gitignore — 파일명에 인명·회의명 포함).
  - 30초 미만 빈 파일 15개 → 건너뜀.
  - **4시간 초과 8개**(최장 360분) → local은 메모리 관리를 위해, 클라우드는 RTZR 4h 제한 때문에 무음 지점 기준 분할 후 이어붙이기 필요(화자 라벨은 청크 간 불연속 — 정리 단계에서 Claude가 맞춤).
  - `YYYYMMDD_주제.MP3`로 이름 붙인 파일과 `REC000NN.MP3`가 섞여 있음. `REC000NN`은 녹음기 시계가 없어 수정일 1601년 → 날짜·주제는 내용에서 추론.
- `rec2md batch <dir> [--engine ...] [--dry-run]`
  - `--dry-run`: ffprobe로 총 재생시간과 엔진별 예상 비용 출력.
  - 처리한 파일은 상태 파일(`.rec2md-state.json`)에 기록해 건너뜀 → 중단 후 재개 가능.
  - 출력은 `<dir>/_transcripts/`에 모음.
- `REC000NN` 파일: 요약 기반으로 `YYYYMMDD?_주제` 제목을 제안하는 `index.md` 생성. 실제 파일명 변경은 사용자 확인 후.
- 비용: 백로그는 studio local로 처리(추가 비용 없음, 처리시간은 파일럿 실측 후 산정). 참고로 RTZR로 전부 돌리면 188시간 ≈ 18〜19만원.

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
- (선택) RTZR 개발자 가입 — 무료 600분을 파일럿 기준점으로 사용
- (선택) NCP CLOVA Speech 도메인 생성, 콘솔에서 단가·무료 크레딧 확인
- Mac: `brew install ffmpeg`, `uv tool install` 후 mlx-whisper·pyannote 설치. Hugging Face 토큰과 pyannote 모델 약관 동의 필요.
- Windows에서 Mac으로 SSH 키 접속 확인

## 위시리스트
- **회의 메모 보정** (2026-10-16 요청): 같은 회의에서 타이핑한 정리물(메모·회의록 초안)이 있으면 녹취를 근거로 보정한다.
  - 메모를 기준 문서로 두고 녹취에서 빠진 논의·결정·수치, 잘못 적힌 발언자·숫자, 메모에만 있고 녹취엔 없는 주장을 표시.
  - 반대로 메모의 인명·용어를 STT 오인식 교정 사전으로 활용(녹취록 품질도 같이 올라감).
  - 형태: `rec2md <audio> --notes <memo.md|docx|hwpx>` → 후처리 단계에서 Claude가 대조. 보정본은 변경 근거(타임스탬프)를 달아 원본 메모와 diff로 볼 수 있게.
- **화자 태깅** (2026-10-16 요청): `화자1`·`화자2` 대신 실명을 붙인다.
  - 자주 등장하는 사람(본인·팀원)의 목소리 샘플로 화자 임베딩(pyannote embedding)을 등록해 두고, 새 녹취의 화자 클러스터와 코사인 유사도로 매칭. 임계값 미만이면 `화자N` 유지.
  - 등록 DB는 개인 데이터라 `~/.config/rec2md/voices/`에 둔다(레포 밖). 처음엔 사람이 확인한 녹취록에서 `화자1=이름` 대응을 받아 그 구간으로 자동 등록.
  - 수동 지정 경로도 둔다: `--names "화자1=홍길동,화자2=..."` 또는 스킬 정리 단계에서 에이전트가 추정한 이름을 확정.
- **클로바 병행 비교 상시화** (2026-10-16): 당분간 클로바노트와 같이 쓰면서 새 녹취마다 `scripts/compare.py`로 화자 일치율·CER을 쌓는다. 유형(전화/근접 회의/원거리 회의/인터뷰)별로 표를 갱신해 클라우드 보조가 필요한 유형을 정한다.

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

## 파일럿 결과 (2026-10-16, studio M2 Max, local vs 클로바노트)
클로바노트 txt 내보내기를 기준점으로 사용(`scripts/compare.py`, 파서 `rec2md.clova_txt`). 클로바도 정답이 아니므로 CER은 "두 엔진 간 차이"다.

| 녹취 유형 | 길이 | 클로바 화자 | local 화자 | 화자 일치율 | CER |
|---|---|---|---|---|---|
| 전화(2인) | 9분 | 2 | 2 | 95.5% | 20.1% |
| 회의(4인, 근접) | 38분 | 4 | 4 | 96.2% | 25.5% |
| 회의(5인, 근접) | 50분 | 5 | 5 | 92.6% | 28.4%* |
| 회의(11인, 원거리·대회의실) | 75분 | 11 | 15 | 70.2% | 52.6% |
| 위원회(9인 발언, 회의 테이블) | 68분 | 9 | 10 | 98.2% | 23.9% |

\* VAD 청킹 이전 수치

- 근접 녹음은 내용이 사실상 같다. CER 차이는 숫자 표기("1 2 3"/"1, 2, 3"), 말 반복("우리가 우리가") 유지 여부, 화자 전환 지점의 단어 1〜2개 귀속에서 주로 나옴. 고유명사는 양쪽 다 틀리는 편(local이 맞힌 경우도 있음: "2첨단").
- 9인 위원회(테이블 위 녹음기)는 근접 회의 수준: 남는 화자 1개는 21초짜리 조각. 원거리 약점은 인원수보다 마이크 거리 문제로 보임. 오인식은 고유명사·신조어(서비스명)에 몰림.
- 원거리 다화자 회의가 약점: 화자 과분할(11→15), 오인식 많음. 이 유형은 클로바/rtzr 보조 대상 후보.
- **VAD 청킹 도입 이유**: 파일 통째 전사 시 원거리 회의에서 Whisper가 문장을 대량으로 건너뜀(분량 클로바 대비 90% → 청킹 후 102%). 화자분리가 찾은 말소리 구간을 2초 미만 간격이면 합쳐 25초 이하 청크로 전사.
  - 합치는 간격 0.5초일 땐 짧은 청크가 많아 전사가 2.8배 느려짐(짧아도 30초 창 비용). 2초로 늘려 원래 속도 근처로 회복.
- 모델: large-v3(비turbo)는 청킹 시 품질 차이가 미미하고 1.4배 느려 기본은 turbo 유지(`--model`로 변경 가능).
- 처리속도(화자분리 포함): 근접 회의 38분 → 약 3분(실시간 12배). 188시간 백로그 ≈ 16시간 추정.
- 클로바노트 구버전 txt 중에는 전사가 아닌 요약본(주요 주제/다음 할 일/요약) 내보내기가 있음 → 비교 불가.

## 검증
1. 짧은 샘플(1~2분)로 엔진마다 `rec2md sample.m4a --engine X`를 실행해 segments.json 스키마가 맞고 화자 라벨이 붙는지 확인.
2. 실제 회의 녹취(30~60분)와 전화 녹취 각 1건으로 엔진 3개를 비교한다. 비교 항목은 인명·전문용어 오인식, 화자 혼동, 처리시간, 비용이고, 표로 정리해서 local 기본 유지 여부와 클라우드를 쓸 유형을 정함.
3. Claude Code에서 "이 녹취 풀어줘 <경로>"라고 하면 스킬이 트리거되어 녹취록.md와 요약까지 한 번에 나오는지 확인.
4. Windows에서 `--remote studio` 왕복이 동작하는지 확인.
