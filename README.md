# rec2md

녹음 파일(회의·전화·강연)을 **화자 구분 녹취록 Markdown + 요약**으로 바꾸는 CLI와 Claude Code 스킬.

- 로컬 STT 우선(Apple Silicon: mlx-whisper + pyannote), 클라우드(RTZR, CLOVA Speech)는 품질 기준점·보조
- 엔진 공통 스키마 `segments.json` → raw Markdown → Claude가 화자 이름 정리·문단화·요약
- 폴더 일괄 처리(중단 후 재개, 4시간 초과 파일 자동 분할)

> 상태: local 엔진(전사 + 화자분리) 동작. 설계는 [`docs/PLAN.md`](docs/PLAN.md).

## 설치 (Apple Silicon Mac)

```sh
brew install ffmpeg
uv tool install --editable '.[diarize]'
```

화자분리(pyannote)는 Hugging Face 토큰이 필요하다.
1. https://hf.co/pyannote/speaker-diarization-community-1 에서 약관 동의
2. https://hf.co/settings/tokens 에서 read 토큰 발급
3. `~/.config/rec2md/.env` 에 `HF_TOKEN=hf_...`

## 사용

```sh
rec2md meeting.m4a                 # → meeting.segments.json, meeting.raw.md
rec2md call.mp3 --speakers 2       # 화자 수를 알면 지정
rec2md talk.mov --no-diarize       # 영상도 됨, 화자분리 생략
```

Claude Code·Codex에서는 스킬(`.claude/skills/rec2md`)이 raw.md를 정리해 `<이름>.녹취록.md`(요약 + 화자 실명 정리)까지 만든다.
