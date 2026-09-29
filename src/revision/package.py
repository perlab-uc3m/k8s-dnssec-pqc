"""Create and verify a portable, checksum-indexed raw campaign archive."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package(campaigns: list[Path], output: Path) -> Path:
    if not campaigns:
        raise ValueError("At least one campaign is required")
    entries = []
    names = set()
    for campaign in campaigns:
        if campaign.name in names:
            raise ValueError(f"Duplicate campaign name: {campaign.name}")
        names.add(campaign.name)
        if not campaign.is_dir() or not (campaign / "campaign.yaml").is_file():
            raise ValueError(f"Incomplete campaign directory: {campaign}")
        for source in sorted(campaign.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"Symlinks are not allowed: {source}")
            if source.is_file():
                rel = Path(campaign.name) / source.relative_to(campaign)
                entries.append(
                    {
                        "path": rel.as_posix(),
                        "bytes": source.stat().st_size,
                        "sha256": _sha(source),
                        "source": source,
                    }
                )
    output.parent.mkdir(parents=True, exist_ok=True)
    index = {
        "schema_version": 1,
        "format": "tar",
        "files": [{key: row[key] for key in ("path", "bytes", "sha256")} for row in entries],
    }
    manifest = output.with_suffix(output.suffix + ".sha256.json")
    with tarfile.open(output, "w") as archive:
        for row in entries:
            archive.add(row["source"], arcname=row["path"], recursive=False)
    index["archive_sha256"] = _sha(output)
    manifest.write_text(json.dumps(index, indent=2) + "\n")
    return manifest


def verify_package(archive_path: Path, manifest: Path) -> int:
    index = json.loads(manifest.read_text())
    if index["schema_version"] != 1 or index["format"] != "tar":
        raise ValueError("Unsupported package manifest")
    if _sha(archive_path) != index["archive_sha256"]:
        raise ValueError("Archive SHA256 mismatch")
    expected = {row["path"]: row for row in index["files"]}
    observed = set()
    with tarfile.open(archive_path, "r") as archive:
        for member in archive:
            if not member.isfile() or member.name not in expected or member.name in observed:
                raise ValueError(f"Unexpected archive entry: {member.name}")
            row = expected[member.name]
            if member.size != row["bytes"]:
                raise ValueError(f"Size mismatch: {member.name}")
            digest = hashlib.sha256()
            stream = archive.extractfile(member)
            assert stream is not None
            while block := stream.read(1024 * 1024):
                digest.update(block)
            if digest.hexdigest() != row["sha256"]:
                raise ValueError(f"SHA256 mismatch: {member.name}")
            observed.add(member.name)
    if observed != set(expected):
        raise ValueError(f"Missing {len(set(expected) - observed)} archive files")
    return len(observed)
