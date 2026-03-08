# Manual Tests

Scripts for manually testing and troubleshooting camera audio playback and recording.

## Setup

1. Copy `.env.example` to `.env` and fill in your credentials:
   ```
   cp .env.example .env
   ```

2. Edit `.env` with your camera password and HA long-lived access token.

Credentials are loaded from environment variables:
- `CAMERA_USER` - camera username (default: `admin`)
- `CAMERA_PASS` - camera password (required)
- `HA_URL` - Home Assistant URL (default: `http://homeassistant.home:8123`)
- `HA_TOKEN` - long-lived access token for HA API calls

## Requirements

- `ffmpeg` - for RTSP recording and audio conversion
- `curl` - for HA API calls
- `python3` - for RTSP backchannel test and analysis scripts
- `matplotlib` + `numpy` - for spectrogram generation (analysis scripts only)

## Scripts

### Playback

**`play_media.sh`** - Play an audio file on a camera speaker via the HA `media_player.play_media` service.
```bash
./play_media.sh media_player.deck_cam Hallelujah-sound-effect.mp3
```

**`rtsp_backchannel_test.py`** - Send audio directly to a camera speaker via RTSP ONVIF backchannel (bypasses HA). Useful for isolating whether the issue is in HA or the camera.
```bash
CAMERA_PASS=changeme python3 rtsp_backchannel_test.py 192.168.253.11 test_tone.aac
```

### Recording

**`record_audio.sh`** - Record audio from a camera's RTSP stream.
```bash
./record_audio.sh 192.168.253.20 15 /tmp/recording.wav
```

**`play_and_record.sh`** - Combined: starts recording from one camera, plays audio on another, then generates a spectrogram for analysis.
```bash
./play_and_record.sh media_player.deck_cam Hallelujah-sound-effect.mp3 192.168.253.20 15
```

### Analysis

**`analyze_recording.py`** - Analyze a WAV recording: prints per-second RMS dB levels and generates a spectrogram image. Requires `matplotlib` and `numpy`.
```bash
python3 analyze_recording.py /tmp/recording.wav "Test description"
```

**`analyze_aac_timing.py`** - Diagnose AAC frame pacing issues. Converts an audio file the same way the HA integration does and reports frame count, duration, and pacing interval.
```bash
python3 analyze_aac_timing.py /tmp/audio.mp3
```

### Test Tone Generation

**`generate_test_tone.py`** - Generate a C major scale test melody as `test_tone.wav` and `test_tone.aac`. The distinct staircase frequency pattern is easy to identify in spectrograms.
```bash
python3 generate_test_tone.py
```

## Troubleshooting Guide

### No sound from camera speaker

1. **Check if audio encoding is enabled** on the camera:
   ```bash
   curl -s --digest -u admin:PASSWORD -g \
     "http://CAMERA_IP/cgi-bin/configManager.cgi?action=getConfig&name=Encode[0].MainFormat[0]" \
     | grep AudioEnable
   ```
   If `AudioEnable=false`, enable it:
   ```bash
   curl -s --digest -u admin:PASSWORD -g \
     "http://CAMERA_IP/cgi-bin/configManager.cgi?action=setConfig&Encode[0].MainFormat[0].AudioEnable=true&Encode[0].ExtraFormat[0].AudioEnable=true"
   ```
   Or use the HA service: `dahua.enable_audio` on the media player entity.

2. **Check RTSP backchannel support** - the DESCRIBE response should include a `sendonly` audio track:
   ```bash
   CAMERA_PASS=changeme python3 rtsp_backchannel_test.py CAMERA_IP test_tone.aac
   ```
   Look for `a=sendonly` in the SDP output. If missing, audio encoding is likely disabled.

3. **Check if audio.cgi is supported** (Lorex cameras typically don't support it):
   ```bash
   curl -s --digest -u admin:PASSWORD \
     "http://CAMERA_IP/cgi-bin/audio.cgi?action=getAudio&httptype=singlepart&channel=0"
   ```
   Connection reset = not supported (falls back to RTSP backchannel).

### Audio plays but sounds distorted

1. **Record and analyze** the output with a nearby camera:
   ```bash
   ./play_and_record.sh media_player.SPEAKER AUDIO_FILE RECORDER_IP 20
   ```

2. **Compare spectrograms** - a clean signal shows clear harmonic bands; distortion shows broadband noise. Use `analyze_recording.py` to generate spectrograms.

3. **Check frame pacing** - if all audio plays in a short burst, frames are being sent too fast:
   ```bash
   python3 analyze_aac_timing.py your_audio.mp3
   ```
   Frame interval should be ~128ms for 8kHz AAC. If it shows 0ms, the duration wasn't parsed correctly.

### Verifying speaker hardware

```bash
# Check if camera reports speaker capability
curl -s --digest -u admin:PASSWORD \
  "http://CAMERA_IP/cgi-bin/devAudioOutput.cgi?action=getCollect"
# result=1 means speaker is present

# Check supported audio codecs
curl -s --digest -u admin:PASSWORD \
  "http://CAMERA_IP/cgi-bin/encode.cgi?action=getConfigCaps&channel=0" \
  | grep Audio.CompressionTypes
```
