#!/usr/bin/env bash
# Record audio from a camera's RTSP stream
#
# Usage: ./record_audio.sh <camera_host> [duration_seconds] [output_file]
# Example: ./record_audio.sh 192.168.253.20 15 /tmp/play-cam-recording.wav

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
fi

HOST="${1:?Usage: $0 <camera_host> [duration_seconds] [output_file]}"
DURATION="${2:-15}"
OUTPUT="${3:-/tmp/camera-recording.wav}"
CAMERA_USER="${CAMERA_USER:-admin}"

if [[ -z "${CAMERA_PASS:-}" ]]; then
    echo "Error: CAMERA_PASS environment variable is required" >&2
    exit 1
fi

RTSP_URL="rtsp://${CAMERA_USER}:${CAMERA_PASS}@${HOST}:554/cam/realmonitor?channel=1&subtype=0"

echo "Recording ${DURATION}s of audio from $HOST to $OUTPUT..."
ffmpeg -y -rtsp_transport tcp \
    -i "$RTSP_URL" \
    -vn -acodec pcm_s16le \
    -t "$DURATION" \
    "$OUTPUT" 2>&1 | tail -5

echo "Recorded: $(ls -lh "$OUTPUT" | awk '{print $5}') — $(file -b "$OUTPUT")"
