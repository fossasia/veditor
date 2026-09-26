"""Unit and integration tests for talk concatenation pipeline module."""

import tempfile
from pathlib import Path
from unittest import mock

import av
import pytest

from app.pipeline.concat import (
    _can_stream_copy,
    concat,
)
from app.storage import LocalDiskBackend
from tests.conftest import (
    FakeStorageBackend,
    assert_playable,
    generate_clip,
    open_and_inspect,
)


def test_concat_cut_alone_passthrough(tmp_path: Path):
    """Verify that calling concat with only cut returns the original cut path (instant zero-cost passthrough)."""
    cut_path = generate_clip(2.0, output_dir=tmp_path)

    # 1. Output path is None
    result = concat(cut_path, intro_path=None, outro_path=None)
    assert result == str(cut_path)

    # 2. String input
    result_str = concat(str(cut_path))
    assert result_str == str(cut_path)

    # 3. Output path matches cut_path.resolve()
    result_same = concat(cut_path, output_path=cut_path)
    assert result_same == str(cut_path)


def test_concat_cut_alone_with_output(tmp_path: Path):
    """Verify that specifying an output path for a cut alone remuxes the file via stream copy."""
    cut_path = generate_clip(2.0, output_dir=tmp_path)
    output_path = tmp_path / "remuxed_cut.mp4"

    result = concat(cut_path, intro_path=None, outro_path=None, output_path=output_path)

    assert result == str(output_path)
    assert output_path.is_file()
    assert_playable(output_path)

    info = open_and_inspect(output_path)
    assert info.duration is not None
    assert abs(info.duration - 2.0) <= 0.3


def test_concat_intro_and_cut(tmp_path: Path):
    """Verify concatenation of intro title slate and cut recording."""
    intro_path = generate_clip(1.0, output_dir=tmp_path)
    cut_path = generate_clip(2.0, output_dir=tmp_path)

    result = concat(cut_path, intro_path=intro_path)
    out_path = Path(result)

    assert out_path.is_file()
    assert_playable(out_path)

    info = open_and_inspect(out_path)
    assert info.duration is not None
    assert abs(info.duration - 3.0) <= 0.35


def test_concat_cut_and_outro(tmp_path: Path):
    """Verify concatenation of cut recording and outro closing slate."""
    cut_path = generate_clip(2.0, output_dir=tmp_path)
    outro_path = generate_clip(1.0, output_dir=tmp_path)

    result = concat(cut_path, outro_path=outro_path)
    out_path = Path(result)

    assert out_path.is_file()
    assert_playable(out_path)

    info = open_and_inspect(out_path)
    assert info.duration is not None
    assert abs(info.duration - 3.0) <= 0.35


def test_concat_intro_cut_outro(tmp_path: Path):
    """Verify concatenation of intro, cut recording, and outro segments."""
    intro_path = generate_clip(1.0, output_dir=tmp_path)
    cut_path = generate_clip(2.0, output_dir=tmp_path)
    outro_path = generate_clip(1.5, output_dir=tmp_path)
    output_path = tmp_path / "full_talk.mp4"

    result = concat(
        cut_path,
        intro_path=intro_path,
        outro_path=outro_path,
        output_path=output_path,
    )

    assert result == str(output_path)
    assert output_path.is_file()
    assert_playable(output_path)

    info = open_and_inspect(output_path)
    assert info.duration is not None
    assert abs(info.duration - 4.5) <= 0.4


def test_concat_resolution_mismatch(tmp_path: Path):
    """Verify that concatenating clips with mismatched resolutions produces valid output matching cut resolution."""
    intro_path = generate_clip(1.0, resolution=(1280, 720), output_dir=tmp_path)
    cut_path = generate_clip(1.5, resolution=(1920, 1080), output_dir=tmp_path)
    output_path = tmp_path / "res_mismatch_output.mp4"

    result = concat(cut_path, intro_path=intro_path, output_path=output_path)

    assert result == str(output_path)
    assert output_path.is_file()
    assert_playable(output_path)

    info = open_and_inspect(output_path)
    assert info.resolution == (1920, 1080)


def test_concat_framerate_mismatch(tmp_path: Path):
    """Verify that concatenating clips with mismatched framerates produces valid 24fps output with proper duration."""
    intro_path = generate_clip(1.0, fps=30, output_dir=tmp_path)
    cut_path = generate_clip(1.5, fps=24, output_dir=tmp_path)
    output_path = tmp_path / "fps_mismatch_output.mp4"

    result = concat(cut_path, intro_path=intro_path, output_path=output_path)

    assert result == str(output_path)
    assert output_path.is_file()
    assert_playable(output_path)

    info = open_and_inspect(output_path)
    assert info.duration is not None
    assert abs(info.duration - 2.5) <= 0.35

    with av.open(output_path) as container:
        v_stream = container.streams.video[0]
        rate = v_stream.average_rate or v_stream.guessed_rate
        assert rate is not None
        assert float(rate) == pytest.approx(24.0, abs=0.1)


def test_concat_sample_rate_mismatch(tmp_path: Path):
    """Verify that concatenating clips with mismatched audio sample rates produces valid 44.1kHz output."""
    intro_path = generate_clip(1.0, sample_rate=48000, output_dir=tmp_path)
    cut_path = generate_clip(1.5, sample_rate=44100, output_dir=tmp_path)
    output_path = tmp_path / "sr_mismatch_output.mp4"

    result = concat(cut_path, intro_path=intro_path, output_path=output_path)

    assert result == str(output_path)
    assert output_path.is_file()
    assert_playable(output_path)

    with av.open(output_path) as container:
        a_stream = container.streams.audio[0]
        assert a_stream.codec_context.sample_rate == 44100


def test_concat_missing_file_raises(tmp_path: Path):
    """Verify FileNotFoundError is raised when any required or optional input file is missing."""
    valid_cut = generate_clip(1.0, output_dir=tmp_path)
    non_existent = tmp_path / "does_not_exist.mp4"

    # Missing cut file
    with pytest.raises(FileNotFoundError, match="Cut file not found"):
        concat(non_existent)

    # Missing intro file
    with pytest.raises(FileNotFoundError, match="Intro file not found"):
        concat(valid_cut, intro_path=non_existent)

    # Missing outro file
    with pytest.raises(FileNotFoundError, match="Outro file not found"):
        concat(valid_cut, outro_path=non_existent)


def test_concat_same_output_path_raises(tmp_path: Path):
    """Verify ValueError is raised if output path matches any input path when intro or outro is present."""
    cut_path = generate_clip(1.0, output_dir=tmp_path)
    intro_path = generate_clip(1.0, output_dir=tmp_path)
    outro_path = generate_clip(1.0, output_dir=tmp_path)

    # Output matches cut_path when intro is present
    with pytest.raises(ValueError, match="Output path matches input path"):
        concat(cut_path, intro_path=intro_path, output_path=cut_path)

    # Output matches intro_path
    with pytest.raises(ValueError, match="Output path matches input path"):
        concat(cut_path, intro_path=intro_path, output_path=intro_path)

    # Output matches outro_path
    with pytest.raises(ValueError, match="Output path matches input path"):
        concat(cut_path, outro_path=outro_path, output_path=outro_path)


def test_concat_force_reencode(tmp_path: Path):
    """Verify that force_reencode=True performs full re-encoding cleanly."""
    intro_path = generate_clip(1.0, output_dir=tmp_path)
    cut_path = generate_clip(1.5, output_dir=tmp_path)
    outro_path = generate_clip(1.0, output_dir=tmp_path)
    output_path = tmp_path / "forced_reencode.mp4"

    result = concat(
        cut_path,
        intro_path=intro_path,
        outro_path=outro_path,
        output_path=output_path,
        force_reencode=True,
    )

    assert result == str(output_path)
    assert output_path.is_file()
    assert_playable(output_path)

    info = open_and_inspect(output_path)
    assert info.duration is not None
    assert abs(info.duration - 3.5) <= 0.4


def test_concat_video_only_intro_pads_silence(tmp_path: Path):
    """Verify that concatenating a video-only intro with a video/audio cut pads silence so both streams align."""
    intro_path = generate_clip(
        1.5, has_video=True, has_audio=False, output_dir=tmp_path
    )
    cut_path = generate_clip(2.0, has_video=True, has_audio=True, output_dir=tmp_path)
    output_path = tmp_path / "video_only_intro_padded.mp4"

    result = concat(cut_path, intro_path=intro_path, output_path=output_path)

    assert result == str(output_path)
    assert output_path.is_file()
    assert_playable(output_path)

    info = open_and_inspect(output_path)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.duration is not None
    assert abs(info.duration - 3.5) <= 0.35


def test_can_stream_copy_validation(tmp_path: Path):
    """Verify _can_stream_copy correctly approves matching clips and rejects mismatched clips."""
    c1 = generate_clip(
        1.0, resolution=(320, 240), fps=24, sample_rate=44100, output_dir=tmp_path
    )
    c2 = generate_clip(
        1.0, resolution=(320, 240), fps=24, sample_rate=44100, output_dir=tmp_path
    )
    c_res = generate_clip(
        1.0, resolution=(640, 480), fps=24, sample_rate=44100, output_dir=tmp_path
    )
    c_fps = generate_clip(
        1.0, resolution=(320, 240), fps=30, sample_rate=44100, output_dir=tmp_path
    )
    c_sr = generate_clip(
        1.0, resolution=(320, 240), fps=24, sample_rate=48000, output_dir=tmp_path
    )

    assert _can_stream_copy([c1, c2]) is True
    assert _can_stream_copy([c1, c_res]) is False
    assert _can_stream_copy([c1, c_fps]) is False
    assert _can_stream_copy([c1, c_sr]) is False
    assert _can_stream_copy([]) is False


def test_concat_audio_only_intro_pads_video_black_frames(tmp_path: Path):
    """Verify that concatenating an audio-only intro with a video/audio cut pads black frames so timelines align."""
    intro_path = generate_clip(
        1.5, has_video=False, has_audio=True, output_dir=tmp_path
    )
    cut_path = generate_clip(2.0, has_video=True, has_audio=True, output_dir=tmp_path)
    output_path = tmp_path / "audio_only_intro_padded.mp4"

    result = concat(cut_path, intro_path=intro_path, output_path=output_path)

    assert result == str(output_path)
    assert output_path.is_file()
    assert_playable(output_path)

    info = open_and_inspect(output_path)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.duration is not None
    assert abs(info.duration - 3.5) <= 0.35


def test_concat_persists_via_storage_backend(tmp_path: Path):
    """Verify that concatenated output is written and persisted via StorageBackend."""
    cut_path = generate_clip(1.5, output_dir=tmp_path)
    intro_path = generate_clip(1.0, output_dir=tmp_path)

    # 1. LocalDiskBackend
    storage_dir = tmp_path / "managed_storage"
    local_backend = LocalDiskBackend(storage_dir)
    key = "talk_42/concat/assembled.mp4"

    result = concat(
        cut_path,
        intro_path=intro_path,
        output_path=key,
        backend=local_backend,
    )

    assert local_backend.exists(key)
    dest_path = local_backend.get(key)
    assert str(dest_path) == result
    assert dest_path.is_file()
    assert_playable(dest_path)

    # 2. FakeStorageBackend
    fake_backend = FakeStorageBackend()
    fake_key = "talk_99/concat/assembled.mp4"
    concat(
        cut_path,
        intro_path=intro_path,
        output_path=fake_key,
        backend=fake_backend,
    )
    assert fake_backend.exists(fake_key)
    assert len(fake_backend.get(fake_key).read_bytes()) > 0


def test_concat_passthrough_persists_via_storage_backend(tmp_path: Path):
    """Verify that cut alone with an explicit destination writes via StorageBackend."""
    cut_path = generate_clip(1.5, output_dir=tmp_path)
    fake_backend = FakeStorageBackend()
    key = "talk_1/cut/passthrough.mp4"

    concat(cut_path, output_path=key, backend=fake_backend)
    assert fake_backend.exists(key)


def test_concat_storage_parameter_uses_get_temp_dir(tmp_path: Path):
    """Verify that passing storage=... uses its get_temp_dir() for scratch space."""
    cut_path = generate_clip(1.0, output_dir=tmp_path)
    intro_path = generate_clip(1.0, output_dir=tmp_path)
    custom_temp = tmp_path / "custom_scratch"
    custom_temp.mkdir()

    storage = LocalDiskBackend(tmp_path / "storage")
    storage.get_temp_dir = mock.Mock(return_value=custom_temp)

    with mock.patch(
        "app.pipeline.concat.tempfile.TemporaryDirectory",
        wraps=tempfile.TemporaryDirectory,
    ) as mock_tempdir:
        concat(cut_path, intro_path=intro_path, storage=storage)
        storage.get_temp_dir.assert_called()
        mock_tempdir.assert_called_once_with(prefix="veditor-concat-", dir=custom_temp)


def test_concat_respects_custom_output_container_format(tmp_path: Path):
    """Verify that specifying a custom container format (e.g. .mkv) creates the matching container."""
    cut_path = generate_clip(1.5, output_dir=tmp_path)
    intro_path = generate_clip(1.0, output_dir=tmp_path)
    output_path = tmp_path / "custom_container.mkv"

    result = concat(cut_path, intro_path=intro_path, output_path=output_path)
    assert result == str(output_path)
    assert output_path.is_file()
    with av.open(str(output_path)) as container:
        assert "matroska" in container.format.name


def test_concat_rejects_invalid_threads(tmp_path: Path):
    """Verify ValueError is raised if threads is non-positive."""
    cut_path = generate_clip(1.0, output_dir=tmp_path)
    intro_path = generate_clip(1.0, output_dir=tmp_path)
    output_path = tmp_path / "out.mp4"

    with pytest.raises(ValueError, match="threads must be positive"):
        concat(cut_path, intro_path=intro_path, output_path=output_path, threads=0)

    with pytest.raises(ValueError, match="threads must be positive"):
        concat(cut_path, intro_path=intro_path, output_path=output_path, threads=-2)
