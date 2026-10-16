# rec2md — 에이전트 작업 메모

- 설계·결정 사항은 `docs/PLAN.md`가 정본. 바뀌면 거기를 고친다.
- **퍼블릭 레포다.** 녹취 원본, 전사 결과, 실제 녹취 파일명, 인명은 커밋하지 않는다. 개인 데이터는 `samples/`, `local/`(gitignore)에만 둔다. 커밋 전 `git diff --cached`로 확인.
- 테스트 픽스처가 필요하면 합성 음성이나 공개 데이터로 만든다.
- 개발 샘플: `samples/`(설명은 `local/SAMPLES.md`). 원본 녹음기는 상시 마운트가 아니므로 샘플은 여기 것을 쓴다.
- 비밀값은 `~/.config/rec2md/.env`. 레포에 두지 않는다.

## 다음 할 일 (2026-10-16 온보딩 시점)
방침은 **로컬 우선**(local이 기본 엔진, rtzr·clova는 품질 기준점·보조).

1. uv 패키지 뼈대 + segments 공통 스키마 + raw.md 렌더러
2. local 엔진(mlx-whisper + pyannote, VAD) — 이 머신(studio)에서 바로 검증. 처리속도 실측
3. batch(`--dry-run`, 상태 파일, 장시간 파일 분할)
4. SKILL.md + 양 머신 심볼릭 링크, `--remote studio`
5. rtzr·clova 엔진 — 키가 먼저 생기는 쪽부터
6. 파일럿 비교(검증 섹션) → 클라우드를 쓸 유형 결정
