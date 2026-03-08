#!/usr/bin/env bash
# Record audio from one camera while playing media on another camera's speaker.
# Generates a spectrogram of the recording for quality analysis.
#
# Usage: ./play_and_record.sh <play_entity> <media_file> <record_host> [duration]
# Example: ./play_and_record.sh media_player.deck_cam Hallelujah-sound-effect.mp3 192.168.253.20 15

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
fi

PLAY_ENTITY="${1:?Usage: $0 <play_entity> <media_file> <record_host> [duration]}"
MEDIA_FILE="${2:?Usage: $0 <play_entity> <media_file> <record_host> [duration]}"
RECORD_HOST="${3:?Usage: $0 <play_entity> <media_file> <record_host> [duration]}"
DURATION="${4:-15}"
CAMERA_USER="${CAMERA_USER:-admin}"
HA_URL="${HA_URL:-http://homeassistant.home:8123}"
OUTPUT="/tmp/camera-recording.wav"
SPECTROGRAM="/tmp/camera-spectrogram.png"

if [[ -z "${CAMERA_PASS:-}" ]]; then
    echo "Error: CAMERA_PASS environment variable is required" >&2
    exit 1
fi
if [[ -z "${HA_TOKEN:-}" ]]; then
    echo "Error: HA_TOKEN environment variable is required" >&2
    exit 1
fi

RTSP_URL="rtsp://${CAMERA_USER}:${CAMERA_PASS}@${RECORD_HOST}:554/cam/realmonitor?channel=1&subtype=0"

# Start recording in background
echo "Starting ${DURATION}s recording from $RECORD_HOST..."
ffmpeg -y -rtsp_transport tcp \
    -i "$RTSP_URL" \
    -vn -acodec pcm_s16le \
    -t "$DURATION" \
    "$OUTPUT" 2>/tmp/ffmpeg-record.log &
RECORD_PID=$!

# Wait for RTSP connection to establish
sleep 2

# Trigger playback
echo "Playing $MEDIA_FILE on $PLAY_ENTITY..."
curl -s -X POST \
    -H "Authorization: Bearer $HA_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
        \"entity_id\": \"$PLAY_ENTITY\",
        \"media_content_id\": \"media-source://media_source/local/$MEDIA_FILE\",
        \"media_content_type\": \"music\"
    }" \
    "$HA_URL/api/services/media_player/play_media" > /dev/null

echo "Waiting for recording to finish..."
wait $RECORD_PID || true

echo "Recording: $(ls -lh "$OUTPUT" | awk '{print $5}')"

# Analyze volume levels
echo ""
echo "=== Volume Analysis ==="
ffmpeg -i "$OUTPUT" -af "volumedetect" -f null /dev/null 2>&1 | grep -E "mean_volume|max_volume"

# Generate spectrogram
echo ""
echo "=== Generating Spectrogram ==="
python3 -c "
import wave, struct, numpy as np
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt

w = wave.open('$OUTPUT', 'r')
frames = w.readframes(w.getnframes())
rate = w.getframerate()
samples = np.array(struct.unpack('<%dh' % w.getnframes(), frames), dtype=float)
w.close()

fig, axes = plt.subplots(2, 1, figsize=(14, 8))
time = np.arange(len(samples)) / rate
axes[0].plot(time, samples, linewidth=0.3)
axes[0].set_title('Waveform')
axes[0].set_xlabel('Time (s)')
axes[0].set_ylabel('Amplitude')
axes[0].set_xlim(0, len(samples)/rate)
axes[1].specgram(samples, Fs=rate, NFFT=256, noverlap=128, cmap='inferno')
axes[1].set_title('Spectrogram')
axes[1].set_xlabel('Time (s)')
axes[1].set_ylabel('Frequency (Hz)')
axes[1].set_ylim(0, 4000)
plt.tight_layout()
plt.savefig('$SPECTROGRAM', dpi=150)
print('Spectrogram saved to $SPECTROGRAM')
" 2>&1 || echo "Spectrogram generation failed (matplotlib/numpy required)"

echo ""
echo "Files:"
echo "  Recording:   $OUTPUT"
echo "  Spectrogram: $SPECTROGRAM"
