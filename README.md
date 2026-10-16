# rec2md

녹음 파일(회의·전화·강연)을 **화자 구분 녹취록 Markdown + 요약**으로 바꾸는 CLI와 Claude Code 스킬.

- 로컬 STT 우선(Apple Silicon: mlx-whisper + pyannote), 클라우드(RTZR, CLOVA Speech)는 품질 기준점·보조
- 엔진 공통 스키마 `segments.json` → raw Markdown → Claude가 화자 이름 정리·문단화·요약
- 폴더 일괄 처리(중단 후 재개, 4시간 초과 파일 자동 분할)

> 상태: 개발 시작 전. 설계는 [`docs/PLAN.md`](docs/PLAN.md).
