#!/usr/bin/env bash
# Play an audio file on a camera speaker via Home Assistant media_player.play_media
#
# Usage: ./play_media.sh <entity_id> <media_file>
# Example: ./play_media.sh media_player.deck_cam Hallelujah-sound-effect.mp3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
fi

ENTITY="${1:?Usage: $0 <entity_id> <media_file>}"
MEDIA_FILE="${2:?Usage: $0 <entity_id> <media_file>}"
HA_URL="${HA_URL:-http://homeassistant.home:8123}"

if [[ -z "${HA_TOKEN:-}" ]]; then
    echo "Error: HA_TOKEN environment variable is required" >&2
    exit 1
fi

echo "Playing $MEDIA_FILE on $ENTITY..."
curl -s -X POST \
    -H "Authorization: Bearer $HA_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
        \"entity_id\": \"$ENTITY\",
        \"media_content_id\": \"media-source://media_source/local/$MEDIA_FILE\",
        \"media_content_type\": \"music\"
    }" \
    "$HA_URL/api/services/media_player/play_media" | python3 -m json.tool

echo "Done."
