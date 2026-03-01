# Agent Notes: Audio Playback Testing

## Test Setup

- **Play camera**: deck-cam (192.168.253.11) — plays audio via `POST /cgi-bin/audio.cgi`
- **Record camera**: play-cam (192.168.253.20) — records via RTSP audio stream
- **Target camera**: front-entry-cam (192.168.253.21) — user's primary test device
- **Test file**: `/tmp/melody_loud.aac` — 12s AAC melody (8kHz, mono, 64kbps ADTS, 95 frames)

### Recording Method

```bash
ffmpeg -rtsp_transport tcp \
  -i rtsp://admin:Password1@192.168.253.20:554/cam/realmonitor?channel=1&subtype=0 \
  -t 18 -vn -acodec pcm_s16le -ar 16000 -ac 1 /tmp/recording.wav
```

Analysis via ffmpeg spectrogram/waveform:
```bash
ffmpeg -i recording.wav -lavfi "showspectrumpic=s=1200x400:mode=combined:color=intensity" spec.png
ffmpeg -i recording.wav -lavfi "showwavespic=s=1200x300:colors=blue" wave.png
```

## Audio API

Endpoint: `POST /cgi-bin/audio.cgi?action=postAudio&httptype={type}&channel=1`

Two `httptype` modes tested:
- `singlepart` — continuous audio bytes in HTTP body
- `multipart` — MIME multipart/x-mixed-replace with per-frame boundaries

Authentication requires digest auth priming: GET to obtain nonce, then POST with pre-computed digest.

## Strategies Tested

### singlepart approaches (all have audible gaps)

| Strategy | Delivery | Result |
|---|---|---|
| Burst (all at once) | 0ms | No audio on streaming cameras |
| Chunked 500ms | HTTP chunked, 25 writes | Gaps + speedups |
| Content-Length 50ms | Raw bytes, 245 writes | Similar gaps |
| Content-Length 20ms | Raw bytes, 613 writes | Similar gaps |
| Frame-aligned 128ms | 1 ADTS frame/write | Similar gaps |
| ffmpeg -re | Codec-level pacing | Similar gaps |
| Pre-buffer + smooth | 2s burst then 10ms writes | Some improvement, still gaps |
| 2x speed | Double real-time rate | Less audio captured |

**Key finding**: All `singlepart` approaches produce similar gap patterns regardless of pacing method. The camera's HTTP body parser doesn't provide smooth data to the audio decoder.

### multipart approaches (significant improvement)

| Frames/part | Parts | Interval | Spectrogram quality |
|---|---|---|---|
| 1 | 95 | 128ms | **Best** — most continuous |
| 2 | 48 | 256ms | Good but less continuous |
| 3 | 32 | 384ms | Degraded |
| 4 | 24 | 512ms | Poor — large gaps |
| 8 | 12 | 1024ms | Poor — buffer starvation |
| 95 (burst) | 95 | 0ms | No audio |

**Winner: `httptype=multipart` with 1 ADTS frame per MIME part, paced at 128ms intervals.**

The camera's multipart parser delivers each MIME part as a discrete audio segment to the decoder, avoiding the buffering issues of singlepart streaming. The camera needs frequent small deliveries (128ms) to keep its playback buffer fed.

### Remaining artifact

User reports minor clicks between notes, likely from MIME boundary parsing overhead (~95 boundary headers in 12 seconds). This is inherent to the multipart protocol and significantly better than singlepart gaps.

## Camera Behavior Differences

- **shed-cam** (IPC-HDW5849HP-ASE-LED): Returns `200 OK` immediately — can handle burst delivery
- **front-entry-cam** (IPC-HFW1841EN-PV): Holds connection open (streaming mode) — requires real-time pacing
- **deck-cam** (Lorex): Similar to front-entry-cam behavior

## Implementation

The `async_post_audio` method in `client.py` should:
1. Parse ADTS frames from the audio data
2. Use `httptype=multipart` in the URL
3. Send each frame as a separate MIME part with `Content-Type: Audio/AAC` and `Content-Length`
4. Pace delivery at 128ms per frame (1024 samples / 8000 Hz)
5. Use digest auth priming (GET then POST with shared nonce)
