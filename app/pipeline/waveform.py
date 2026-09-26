"""Waveform peak extraction from media files using PyAV and NumPy."""

from pathlib import Path

import av
import numpy as np


def extract_waveform_peaks(input_path: Path, num_peaks: int = 1000) -> list[float]:
    """
    Extract normalized peak audio amplitude values from a media file.

    Returns a list of float values bounded in [0.0, 1.0] representing the audio envelope,
    ideal for rendering in a web timeline or waveform visualizer.
    """
    chunks: list[np.ndarray] = []
    try:
        with av.open(str(input_path)) as container:
            if not container.streams.audio:
                return []
            resampler = av.AudioResampler(format="s16", layout="mono", rate=1000)
            for frame in container.decode(container.streams.audio[0]):
                for resampled in resampler.resample(frame):
                    arr = resampled.to_ndarray()
                    if arr.size > 0:
                        chunks.append(arr.flatten())
            for resampled in resampler.resample(None):
                arr = resampled.to_ndarray()
                if arr.size > 0:
                    chunks.append(arr.flatten())
    except av.FFmpegError, ValueError, RuntimeError, OSError:
        return []

    if not chunks:
        return []

    samples = np.abs(np.concatenate(chunks).astype(np.float32) / 32768.0)
    if samples.size == 0:
        return []

    sampled = (
        [float(np.max(c)) for c in np.array_split(samples, num_peaks)]
        if samples.size >= num_peaks
        else [float(s) for s in samples]
    )

    max_p = max(sampled) if sampled else 0.0
    scale = (1.0 / max_p) if max_p > 0.01 else 1.0
    return [round(min(1.0, p * scale), 4) for p in sampled]
