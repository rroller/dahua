#!/usr/bin/env python3
"""Analyze a WAV recording: per-second RMS levels and spectrogram.

Usage: python3 analyze_recording.py <wav_file> [title]
Example: python3 analyze_recording.py /tmp/recording.wav "Deck-cam test"

Requires matplotlib and numpy (pip install matplotlib numpy).
"""

import math
import struct
import sys
import wave

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    wav_path = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else wav_path

    w = wave.open(wav_path, "r")
    frames = w.readframes(w.getnframes())
    rate = w.getframerate()
    samples = np.array(struct.unpack("<%dh" % w.getnframes(), frames), dtype=float)
    w.close()

    duration = len(samples) / rate
    rms_total = math.sqrt(np.mean(samples**2))
    db_total = 20 * math.log10(max(rms_total, 1) / 32768)

    print(f"File: {wav_path}")
    print(f"Duration: {duration:.1f}s, Sample rate: {rate}Hz, Samples: {len(samples)}")
    print(f"Overall RMS: {rms_total:.1f} ({db_total:.1f} dBFS)")
    print(f"Min: {samples.min():.0f}, Max: {samples.max():.0f}")
    print()
    print("=== Per-second RMS levels ===")
    for i in range(0, len(samples), rate):
        chunk = samples[i : i + rate]
        chunk_rms = math.sqrt(np.mean(chunk**2))
        db = 20 * math.log10(max(chunk_rms, 1) / 32768)
        bar = "#" * max(1, int((db + 70) * 2))
        print(f"  {i // rate:2d}s: {db:6.1f} dBFS  {bar}")

    # Generate spectrogram
    out_path = wav_path.rsplit(".", 1)[0] + "-spectrogram.png"
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))

    time = np.arange(len(samples)) / rate
    axes[0].plot(time, samples, linewidth=0.3)
    axes[0].set_title(f"{title}: Waveform")
    axes[0].set_xlabel("Time (s)")
    axes[0].set_ylabel("Amplitude")
    axes[0].set_xlim(0, duration)

    axes[1].specgram(samples, Fs=rate, NFFT=256, noverlap=128, cmap="inferno")
    axes[1].set_title(f"{title}: Spectrogram")
    axes[1].set_xlabel("Time (s)")
    axes[1].set_ylabel("Frequency (Hz)")
    axes[1].set_ylim(0, 4000)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"\nSpectrogram saved to {out_path}")


if __name__ == "__main__":
    main()
