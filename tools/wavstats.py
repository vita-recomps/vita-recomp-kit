"""Loudness of a recorded run over time (16-bit PCM WAV, as tools/proccap writes).

Prints one line per window: start time, RMS and peak in dBFS, and the share of
"onsets" (sudden rises in short-term energy, which sound effects such as sword
hits and footsteps make, and steady music mostly does not).

usage:
  python -I wavstats.py <run.wav> [window seconds, default 5] [csv out]
"""
import sys
import wave

import numpy as np


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    window = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    with wave.open(path, 'rb') as w:
        rate, channels = w.getframerate(), w.getnchannels()
    # read the samples straight from the file: a recording still in progress has a data size of 0
    # in its header (proccap writes it at the end)
    with open(path, 'rb') as f:
        raw = f.read()[44:]
    raw = raw[:len(raw) // (2 * channels) * 2 * channels]
    pcm = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768
    mono = pcm.reshape(-1, channels).mean(axis=1)

    # short-term energy every 10 ms; an onset is a frame 6 dB above the previous 100 ms's mean
    hop = rate // 100
    n = len(mono) // hop
    energy = (mono[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-10
    kernel = np.ones(10) / 10
    background = np.convolve(energy, kernel, mode='full')[:n]
    background = np.concatenate(([energy[0]], background[:-1]))
    onset = (energy > 4 * background) & (energy > 1e-5)

    rows = []
    per = int(window * 100)
    for i in range(0, n, per):
        e = energy[i:i + per]
        seg = mono[i * hop:(i + per) * hop]
        rms = 10 * np.log10(e.mean())
        peak = 20 * np.log10(np.abs(seg).max() + 1e-10)
        rows.append((i / 100, rms, peak, int(onset[i:i + per].sum())))
    print(f'{path}: {len(mono) / rate:.1f} s, {rate} Hz, {channels} ch')
    print(' time     rms dB  peak dB  onsets')
    for t, rms, peak, ons in rows:
        print(f'{t:6.0f}  {rms:7.1f}  {peak:7.1f}  {ons:6d}')
    if len(sys.argv) > 3:
        with open(sys.argv[3], 'w') as f:
            f.write('time,rms_db,peak_db,onsets\n')
            for r in rows:
                f.write('%.2f,%.2f,%.2f,%d\n' % r)


if __name__ == '__main__':
    main()
