#!/usr/bin/env bash
set -euo pipefail

# macOS say (한국어 Yuna 음성 + 속도 조절) 및 ffmpeg를 사용하여 합성 대화 음원 생성
# 출력: tests/fixtures/sample_dialogue.wav (16kHz mono 16bit PCM)

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${DIR}/sample_dialogue.wav"

if ! command -v say >/dev/null 2>&1 || ! command -v ffmpeg >/dev/null 2>&1; then
    echo "say 또는 ffmpeg가 필요합니다." >&2
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# 화자 1: 기본 속도 (Yuna 180wpm)
say -v Yuna -r 180 "오늘 회의를 시작하겠습니다." -o "${TMP}/spk1.aiff"

# 화자 2: 다른 속도 (Yuna 140wpm)
say -v Yuna -r 140 "네, 첫 번째 안건입니다." -o "${TMP}/spk2.aiff"

# 화자 3: 빠른 속도 (Yuna 210wpm)
say -v Yuna -r 210 "진행해 주시기 바랍니다." -o "${TMP}/spk3.aiff"

cat <<EOF > "${TMP}/concat.txt"
file '${TMP}/spk1.aiff'
file '${TMP}/spk2.aiff'
file '${TMP}/spk3.aiff'
EOF

ffmpeg -nostdin -hide_banner -loglevel error -y \
  -f concat -safe 0 -i "${TMP}/concat.txt" \
  -ac 1 -ar 16000 -c:a pcm_s16le "${OUT}"

echo "Generated: ${OUT} ($(wc -c < "${OUT}") bytes)"
