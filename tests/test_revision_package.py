import pytest

from src.revision.package import package, verify_package


def test_archive_verification_detects_corruption(tmp_path):
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "campaign.yaml").write_text("schema_version: 1\n")
    (campaign / "raw.bin").write_bytes(b"original DNS packet bytes")
    archive = tmp_path / "data.tar"
    manifest = package([campaign], archive)
    assert verify_package(archive, manifest) == 2
    with archive.open("r+b") as stream:
        stream.seek(1024)
        stream.write(b"changed")
    with pytest.raises(ValueError, match="Archive SHA256 mismatch"):
        verify_package(archive, manifest)
