# Manual Tests

Scripts for manually testing camera audio playback and recording via RTSP.

## Setup

Credentials are loaded from environment variables:
- `CAMERA_USER` - camera username (default: `admin`)
- `CAMERA_PASS` - camera password (required)

Or export them in a `.env` file (gitignored).

## Scripts

- `play_media.sh` - Play an audio file on a camera speaker via HA API
- `record_audio.sh` - Record audio from a camera's RTSP stream

## Requirements

- `ffmpeg` for RTSP recording
- `curl` for HA API calls
- `HA_TOKEN` env var with a long-lived Home Assistant access token
- `HA_URL` env var (default: `http://homeassistant.home:8123`)
