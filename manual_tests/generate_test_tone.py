#!/usr/bin/env python3
"""Generate a simple test melody as WAV and AAC files.

Produces a short melody using pure sine tones (C major scale + chord)
that's easy to identify in a spectrogram.

Output:
  test_tone.wav - 16-bit PCM, 8kHz mono
  test_tone.aac - AAC ADTS, 8kHz mono (requires ffmpeg)
"""

import math
import struct
import subprocess
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 8000
AMPLITUDE = 16000

# Notes as (frequency_hz, duration_seconds)
# C major scale up, then a chord
MELODY = [
    (523, 0.3),  # C5
    (587, 0.3),  # D5
    (659, 0.3),  # E5
    (698, 0.3),  # F5
    (784, 0.3),  # G5
    (880, 0.3),  # A5
    (988, 0.3),  # B5
    (1047, 0.6),  # C6 (held)
    (0, 0.2),  # silence
    (523, 0.4),  # C5 again
    (659, 0.4),  # E5
    (784, 0.4),  # G5
    (1047, 0.8),  # C6 (held longer)
]


def generate_tone(
    freq: float, duration: float, sample_rate: int = SAMPLE_RATE
) -> list[int]:
    """Generate samples for a sine wave tone."""
    n_samples = int(sample_rate * duration)
    if freq == 0:
        return [0] * n_samples
    samples = []
    # Apply fade in/out to avoid clicks (10ms)
    fade_samples = min(int(sample_rate * 0.01), n_samples // 2)
    for i in range(n_samples):
        value = math.sin(2 * math.pi * freq * i / sample_rate)
        # Fade envelope
        if i < fade_samples:
            value *= i / fade_samples
        elif i > n_samples - fade_samples:
            value *= (n_samples - i) / fade_samples
        samples.append(int(value * AMPLITUDE))
    return samples


def main():
    out_dir = Path(__file__).parent
    wav_path = out_dir / "test_tone.wav"
    aac_path = out_dir / "test_tone.aac"

    # Generate all samples
    all_samples = []
    for freq, dur in MELODY:
        all_samples.extend(generate_tone(freq, dur))

    duration = len(all_samples) / SAMPLE_RATE
    print(f"Generated {len(all_samples)} samples ({duration:.1f}s) at {SAMPLE_RATE}Hz")

    # Write WAV
    with wave.open(str(wav_path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(struct.pack(f"<{len(all_samples)}h", *all_samples))
    print(f"Wrote {wav_path} ({wav_path.stat().st_size} bytes)")

    # Convert to AAC via ffmpeg
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(wav_path),
                "-acodec",
                "aac",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-b:a",
                "32k",
                "-f",
                "adts",
                str(aac_path),
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            print(f"Wrote {aac_path} ({aac_path.stat().st_size} bytes)")
        else:
            print(f"ffmpeg failed: {result.stderr.decode()[-200:]}", file=sys.stderr)
    except FileNotFoundError:
        print("ffmpeg not found, skipping AAC conversion", file=sys.stderr)


if __name__ == "__main__":
    main()
