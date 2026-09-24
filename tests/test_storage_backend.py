"""Cloud Storage backend detection tests without credentials or network access."""

from pathlib import Path

import pytest

import ste.runs.backend as backend_module
from ste.runs.backend import LeaseUnavailableError, detect_gcs_mount, get_backend
from tests.fake_gcs import FakeBucket


def _mountinfo(tmp_path: Path, text: str) -> Path:
    """Write mountinfo fixture text and return its isolated filesystem path."""
    # A real file exercises the same decoding and malformed-line handling as /proc.
    path = tmp_path / "mountinfo"
    path.write_text(text, encoding="utf-8")
    return path


def test_detect_cloud_run_mount_and_subdirectory_prefix(tmp_path):
    """Extract the bucket and relative object prefix from a gcsfuse mount line."""
    root = tmp_path / "experiments"
    nested = root / "team" / "runs"
    nested.mkdir(parents=True)
    line = f"118 111 0:57 / {root} rw - fuse.gcsfuse ste-experiments rw\n"
    mount = detect_gcs_mount(nested, _mountinfo(tmp_path, line))
    # The longest containing mount maps nested files beneath a trailing-slash prefix.
    assert mount is not None
    assert mount.bucket_name == "ste-experiments" and mount.prefix == "team/runs/"


def test_detect_decodes_escaped_space_and_ignores_bad_lines(tmp_path):
    """Decode octal mount paths while ignoring malformed and non-gcsfuse entries."""
    root = tmp_path / "space here"
    root.mkdir()
    escaped = str(root).replace(" ", r"\040")
    text = (
        f"broken\n1 2 3 4 {root} rw - ext4 disk rw\n1 2 3 4 {escaped} rw - fuse.gcsfuse bucket rw\n"
    )
    mount = detect_gcs_mount(root, _mountinfo(tmp_path, text))
    # Only the decoded Cloud Storage mount qualifies for object coordination.
    assert mount is not None
    assert mount.mount_point == root.resolve() and mount.prefix == ""


def test_backend_refuses_missing_gcsfuse_outside_cloud_run(tmp_path):
    """Reject missing gcsfuse even when no Cloud Run environment marker exists."""
    mountinfo = _mountinfo(tmp_path, "1 2 3 4 / rw - ext4 disk rw\n")
    # Web coordination deliberately has no local-filesystem backend.
    with pytest.raises(LeaseUnavailableError, match="required Cloud Storage volume"):
        get_backend(tmp_path / "not-mounted", mountinfo)


def test_backend_refuses_malformed_mountinfo_without_gcsfuse(tmp_path):
    """Reject unsafe coordination when mountinfo has no valid gcsfuse entry."""
    mountinfo = _mountinfo(tmp_path, "malformed\n")
    # The safe message contains no mount contents, bucket data, or credentials.
    with pytest.raises(LeaseUnavailableError, match="required Cloud Storage volume"):
        get_backend(tmp_path / "cloud-run", mountinfo)


def test_mapping_probe_accepts_identical_api_contents(tmp_path, monkeypatch):
    """Accept a closed mount write only when the API returns identical bytes."""
    bucket = FakeBucket()
    mount = backend_module.GCSMount(tmp_path, "bucket", "prefix/")

    def mirror_probe(_bucket, object_name):
        """Mirror the mounted probe into the fake API and return its generation."""
        relative = object_name.removeprefix("prefix/")
        contents = (tmp_path / relative).read_bytes()
        blob = bucket.blob(object_name)
        blob.upload_from_string(contents, content_type="text/plain", if_generation_match=0)
        return contents, blob.generation

    monkeypatch.setattr(backend_module, "read_blob", mirror_probe)
    # Successful verification also conditionally deletes the API probe object.
    backend_module._verify_mapping(tmp_path, mount, bucket)
    assert not bucket.objects


@pytest.mark.parametrize("api_result", [FileNotFoundError("missing"), (b"different", 1)])
def test_mapping_probe_rejects_missing_or_different_object(tmp_path, monkeypatch, api_result):
    """Reject absent or mismatched API observations of a mounted probe file."""
    bucket = FakeBucket()
    mount = backend_module.GCSMount(tmp_path, "bucket", "")

    def bad_read(_bucket, _object_name):
        """Return the configured unsafe mapping result without contacting a service."""
        if isinstance(api_result, Exception):
            raise api_result
        return api_result

    monkeypatch.setattr(backend_module, "read_blob", bad_read)
    # All mapping failures collapse to one sanitized operational error.
    with pytest.raises(LeaseUnavailableError, match="verification failed"):
        backend_module._verify_mapping(tmp_path, mount, bucket)
