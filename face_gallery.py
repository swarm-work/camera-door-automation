#!/usr/bin/env python3
"""Export, verify, and stage restores of Frigate's approved face library."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Mapping
from urllib.parse import quote

import boto3
from botocore.config import Config
from dotenv import load_dotenv

LOG = logging.getLogger("face_gallery")
SCHEMA_VERSION = 1
DEFAULT_PREFIX = "face-gallery/v1"
ARCHIVE_NAME = "face-gallery.zip"
MANIFEST_NAME = "manifest.json"
COMPLETE_NAME = "COMPLETE"
SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_COPY_CHUNK = 1024 * 1024


class GalleryError(RuntimeError):
    """An expected, user-actionable gallery operation failure."""


@dataclass(frozen=True)
class Settings:
    source_dir: Path
    bucket: str
    prefix: str
    kms_key_id: str | None
    aws_profile: str | None
    bfr_db: Path | None
    region: str
    endpoint_url: str | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        recordings = _path_value(values.get("FREGATA_RECORDINGS_DIR", "~/Fregata/media/recordings"))
        source_value = values.get("FACE_GALLERY_SOURCE_DIR")
        source = _path_value(source_value) if source_value else recordings.parent / "clips" / "faces"
        bucket = (values.get("FACE_GALLERY_BUCKET") or values.get("S3_BUCKET") or "").strip()
        prefix = (values.get("FACE_GALLERY_PREFIX") or DEFAULT_PREFIX).strip("/")
        if not prefix:
            raise GalleryError("FACE_GALLERY_PREFIX must not be empty")
        db_value = (values.get("FACE_GALLERY_BFR_DB") or "").strip()
        return cls(
            source_dir=source,
            bucket=bucket,
            prefix=prefix,
            kms_key_id=(values.get("FACE_GALLERY_KMS_KEY_ID") or "").strip() or None,
            aws_profile=(values.get("FACE_GALLERY_AWS_PROFILE") or values.get("AWS_PROFILE") or "").strip() or None,
            bfr_db=_path_value(db_value) if db_value else None,
            region=(values.get("AWS_REGION") or "us-east-1").strip(),
            endpoint_url=(values.get("S3_ENDPOINT_URL") or "").strip() or None,
        )


@dataclass(frozen=True)
class SourceFile:
    source: Path
    archive_path: str
    kind: str


@dataclass(frozen=True)
class ExportBundle:
    export_id: str
    archive_path: Path
    archive_sha256: str
    archive_size: int
    manifest: dict[str, object]
    manifest_bytes: bytes
    manifest_sha256: str


@dataclass(frozen=True)
class VerifiedExport:
    export_id: str
    manifest: dict[str, object]
    archive_path: Path


def _path_value(value: str) -> Path:
    return Path(os.path.expanduser(value))


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as src:
            while chunk := src.read(_COPY_CHUNK):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise GalleryError("export content became unreadable") from exc
    return digest.hexdigest(), size


def sha256_stream(body: BinaryIO, destination: BinaryIO | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := body.read(_COPY_CHUNK):
        digest.update(chunk)
        size += len(chunk)
        if destination is not None:
            destination.write(chunk)
    return digest.hexdigest(), size


def collect_approved_images(source_dir: Path) -> list[SourceFile]:
    """Return supported regular images below visible, non-symlink person dirs."""
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise GalleryError("FACE_GALLERY_SOURCE_DIR must be a non-symlink directory")

    selected: list[SourceFile] = []
    try:
        people = sorted(source_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise GalleryError("could not read FACE_GALLERY_SOURCE_DIR") from exc

    for person_dir in people:
        if person_dir.name.startswith(".") or person_dir.name.casefold() == "train":
            continue
        try:
            if person_dir.is_symlink() or not person_dir.is_dir():
                continue
        except OSError:
            continue
        _collect_person_images(source_dir, person_dir, selected)

    selected.sort(key=lambda item: item.archive_path)
    return selected


def _collect_person_images(source_dir: Path, directory: Path, selected: list[SourceFile]) -> None:
    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise GalleryError("could not read an approved person directory") from exc
    for child in children:
        if child.name.startswith(".") or child.name.casefold() == "train":
            continue
        try:
            if child.is_symlink():
                continue
            if child.is_dir():
                _collect_person_images(source_dir, child, selected)
                continue
            mode = child.stat(follow_symlinks=False).st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and child.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES:
            relative = child.relative_to(source_dir).as_posix()
            selected.append(SourceFile(child, f"faces/{relative}", "approved_face"))


def backup_sqlite(source: Path, destination: Path) -> None:
    if source.name != "bfr.db" or not source.is_file() or source.is_symlink():
        raise GalleryError("FACE_GALLERY_BFR_DB must point to an existing regular bfr.db")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{quote(str(source.resolve()))}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source_db:
            with sqlite3.connect(destination) as backup_db:
                source_db.backup(backup_db)
    except sqlite3.Error as exc:
        raise GalleryError("could not create a consistent bfr.db backup") from exc


def _identity(entries: Iterable[dict[str, object]]) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "files": list(entries)}


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def build_export(settings: Settings, work_dir: Path) -> ExportBundle:
    """Create deterministic local export artifacts without contacting S3."""
    work_dir.mkdir(parents=True, exist_ok=True)
    sources = collect_approved_images(settings.source_dir)
    if not sources:
        raise GalleryError("no approved face images found")

    if settings.bfr_db is not None:
        db_backup = work_dir / "sqlite-backup" / "bfr.db"
        backup_sqlite(settings.bfr_db, db_backup)
        sources.append(SourceFile(db_backup, "database/bfr.db", "bfr_database"))

    entries: list[dict[str, object]] = []
    for item in sources:
        digest, size = sha256_file(item.source)
        entries.append({"kind": item.kind, "path": item.archive_path, "sha256": digest, "size": size})
    entries.sort(key=lambda entry: str(entry["path"]))

    export_id = hashlib.sha256(canonical_json(_identity(entries))).hexdigest()
    archive_path = work_dir / ARCHIVE_NAME
    source_by_name = {item.archive_path: item.source for item in sources}
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for entry in entries:
                name = str(entry["path"])
                with source_by_name[name].open("rb") as src, archive.open(_zip_info(name), "w") as dst:
                    shutil.copyfileobj(src, dst, length=_COPY_CHUNK)
    except OSError as exc:
        raise GalleryError("could not create the local export archive") from exc

    archive_sha256, archive_size = sha256_file(archive_path)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "export_id": export_id,
        "archive": {"name": ARCHIVE_NAME, "sha256": archive_sha256, "size": archive_size},
        "files": entries,
    }
    manifest_bytes = canonical_json(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    validate_archive(archive_path, manifest)
    return ExportBundle(
        export_id=export_id,
        archive_path=archive_path,
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
    )


def make_s3_client(settings: Settings):
    session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.region)
    return session.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        config=Config(retries={"max_attempts": 8, "mode": "standard"}),
    )


def encryption_args(settings: Settings) -> dict[str, str]:
    if settings.kms_key_id:
        return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": settings.kms_key_id}
    return {"ServerSideEncryption": "AES256"}


def export_keys(settings: Settings, export_id: str) -> dict[str, str]:
    if len(export_id) != 64 or any(char not in "0123456789abcdef" for char in export_id):
        raise GalleryError("invalid export ID")
    base = f"{settings.prefix}/exports/{export_id}"
    return {
        "archive": f"{base}/{ARCHIVE_NAME}",
        "manifest": f"{base}/{MANIFEST_NAME}",
        "complete": f"{base}/{COMPLETE_NAME}",
    }


def _put_bytes(client, settings: Settings, key: str, body: bytes, content_type: str) -> None:
    digest = hashlib.sha256(body).hexdigest()
    client.put_object(
        Bucket=settings.bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        Metadata={"sha256": digest},
        **encryption_args(settings),
    )


def _upload_archive(client, settings: Settings, key: str, bundle: ExportBundle) -> None:
    extra = {
        "ContentType": "application/zip",
        "Metadata": {"sha256": bundle.archive_sha256},
        **encryption_args(settings),
    }
    client.upload_file(str(bundle.archive_path), settings.bucket, key, ExtraArgs=extra)


def _read_remote(
    client,
    settings: Settings,
    key: str,
    destination: Path | None = None,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> tuple[bytes | None, str, int]:
    try:
        head = client.head_object(Bucket=settings.bucket, Key=key)
    except Exception as exc:
        raise GalleryError("export object is missing or unreadable") from exc

    head_size = int(head.get("ContentLength", -1))
    if expected_size is not None and head_size != expected_size:
        raise GalleryError("export object size does not match its manifest")
    metadata_hash = (head.get("Metadata") or {}).get("sha256")
    if expected_sha256 is not None and metadata_hash != expected_sha256:
        raise GalleryError("export object SHA-256 metadata does not match")
    if settings.kms_key_id:
        if (
            head.get("ServerSideEncryption") != "aws:kms"
            or head.get("SSEKMSKeyId") != settings.kms_key_id
        ):
            raise GalleryError("export object is not encrypted with the configured KMS key")
    elif head.get("ServerSideEncryption") != "AES256":
        raise GalleryError("export object is not encrypted with SSE-S3 AES256")
    try:
        response = client.get_object(Bucket=settings.bucket, Key=key)
    except Exception as exc:
        raise GalleryError("export object is missing or unreadable") from exc

    body = response["Body"]
    if destination is None:
        data = body.read()
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
    else:
        with destination.open("wb") as dst:
            digest, size = sha256_stream(body, dst)
        data = None
    close = getattr(body, "close", None)
    if close is not None:
        close()
    if not isinstance(metadata_hash, str) or metadata_hash != digest:
        raise GalleryError("export object SHA-256 metadata does not match its bytes")

    if size != head_size:
        raise GalleryError("export object changed while it was downloaded")
    if expected_size is not None and size != expected_size:
        raise GalleryError("downloaded object size does not match")
    if expected_sha256 is not None and digest != expected_sha256:
        raise GalleryError("downloaded object SHA-256 does not match")
    return data, digest, size


def _parse_json_object(data: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GalleryError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise GalleryError(f"{label} must be a JSON object")
    return value


def _manifest_entries(manifest: Mapping[str, object]) -> list[dict[str, object]]:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise GalleryError("unsupported manifest schema version")
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise GalleryError("manifest contains no files")
    entries: list[dict[str, object]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise GalleryError("manifest file entry is invalid")
        if set(raw) != {"kind", "path", "sha256", "size"}:
            raise GalleryError("manifest file entry has unexpected fields")
        path = raw.get("path")
        digest = raw.get("sha256")
        size = raw.get("size")
        kind = raw.get("kind")
        if not isinstance(path, str) or not _safe_archive_name(path):
            raise GalleryError("manifest contains an unsafe archive path")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise GalleryError("manifest contains an invalid SHA-256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise GalleryError("manifest contains an invalid file size")
        if kind not in {"approved_face", "bfr_database"}:
            raise GalleryError("manifest contains an invalid file kind")
        parts = PurePosixPath(path).parts
        if kind == "approved_face":
            if (
                len(parts) < 3
                or parts[0] != "faces"
                or PurePosixPath(parts[-1]).suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES
                or any(part.startswith(".") or part.casefold() == "train" for part in parts[1:])
            ):
                raise GalleryError("manifest contains a non-approved face path")
        elif path != "database/bfr.db":
            raise GalleryError("manifest contains an invalid database path")
        entries.append(dict(raw))
    if [entry["path"] for entry in entries] != sorted(entry["path"] for entry in entries):
        raise GalleryError("manifest file entries are not sorted")
    if len({entry["path"] for entry in entries}) != len(entries):
        raise GalleryError("manifest contains duplicate paths")
    if sum(entry["kind"] == "bfr_database" for entry in entries) > 1:
        raise GalleryError("manifest contains multiple database backups")
    return entries


def _validate_manifest(manifest: dict[str, object], export_id: str) -> tuple[str, int, list[dict[str, object]]]:
    if set(manifest) != {"schema_version", "export_id", "archive", "files"}:
        raise GalleryError("manifest has unexpected fields")
    entries = _manifest_entries(manifest)
    computed_id = hashlib.sha256(canonical_json(_identity(entries))).hexdigest()
    if manifest.get("export_id") != export_id or computed_id != export_id:
        raise GalleryError("manifest content does not match the export ID")
    archive = manifest.get("archive")
    if (
        not isinstance(archive, dict)
        or set(archive) != {"name", "sha256", "size"}
        or archive.get("name") != ARCHIVE_NAME
    ):
        raise GalleryError("manifest archive description is invalid")
    digest = archive.get("sha256")
    size = archive.get("size")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise GalleryError("manifest archive SHA-256 is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise GalleryError("manifest archive size is invalid")
    return digest, size, entries


def _safe_archive_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def validate_archive(archive_path: Path, manifest: Mapping[str, object]) -> None:
    entries = _manifest_entries(manifest)
    expected = {str(entry["path"]): entry for entry in entries}
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise GalleryError("archive members do not match the manifest")
            for info in infos:
                if not _safe_archive_name(info.filename) or info.is_dir():
                    raise GalleryError("archive contains an unsafe member")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                    raise GalleryError("archive contains a non-regular member")
                entry = expected[info.filename]
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, "r") as src:
                    while chunk := src.read(_COPY_CHUNK):
                        digest.update(chunk)
                        size += len(chunk)
                if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise GalleryError("archive member does not match the manifest")
    except (OSError, zipfile.BadZipFile) as exc:
        raise GalleryError("archive is not a valid ZIP file") from exc


def publish_export(client, settings: Settings, bundle: ExportBundle) -> None:
    if not settings.bucket:
        raise GalleryError("FACE_GALLERY_BUCKET or S3_BUCKET is required with --apply")
    keys = export_keys(settings, bundle.export_id)
    _upload_archive(client, settings, keys["archive"], bundle)
    _put_bytes(client, settings, keys["manifest"], bundle.manifest_bytes, "application/json")

    _read_remote(
        client, settings, keys["archive"],
        expected_sha256=bundle.archive_sha256, expected_size=bundle.archive_size,
    )
    _read_remote(
        client, settings, keys["manifest"],
        expected_sha256=bundle.manifest_sha256, expected_size=len(bundle.manifest_bytes),
    )

    marker = canonical_json({
        "schema_version": SCHEMA_VERSION,
        "export_id": bundle.export_id,
        "archive_sha256": bundle.archive_sha256,
        "manifest_sha256": bundle.manifest_sha256,
    })
    _put_bytes(client, settings, keys["complete"], marker, "application/json")
    with tempfile.TemporaryDirectory(prefix="face-gallery-publish-") as directory:
        verify_remote(client, settings, bundle.export_id, Path(directory))


def verify_remote(client, settings: Settings, export_id: str, work_dir: Path) -> VerifiedExport:
    """Require COMPLETE and verify remote metadata, bytes, IDs, and archive members."""
    if not settings.bucket:
        raise GalleryError("FACE_GALLERY_BUCKET or S3_BUCKET is required")
    keys = export_keys(settings, export_id)
    marker_bytes, _, _ = _read_remote(client, settings, keys["complete"])
    assert marker_bytes is not None
    marker = _parse_json_object(marker_bytes, "completion marker")
    if set(marker) != {"schema_version", "export_id", "archive_sha256", "manifest_sha256"}:
        raise GalleryError("completion marker has unexpected fields")
    if marker.get("schema_version") != SCHEMA_VERSION or marker.get("export_id") != export_id:
        raise GalleryError("completion marker does not match the requested export")
    manifest_hash = marker.get("manifest_sha256")
    archive_hash = marker.get("archive_sha256")
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
        for value in (manifest_hash, archive_hash)
    ):
        raise GalleryError("completion marker is missing valid hashes")

    manifest_bytes, _, _ = _read_remote(
        client, settings, keys["manifest"], expected_sha256=manifest_hash,
    )
    assert manifest_bytes is not None
    manifest = _parse_json_object(manifest_bytes, "manifest")
    expected_archive_hash, expected_archive_size, _ = _validate_manifest(manifest, export_id)
    if archive_hash != expected_archive_hash:
        raise GalleryError("completion marker and manifest archive hashes differ")

    work_dir.mkdir(parents=True, exist_ok=True)
    archive_path = work_dir / ARCHIVE_NAME
    _read_remote(
        client, settings, keys["archive"], destination=archive_path,
        expected_sha256=expected_archive_hash, expected_size=expected_archive_size,
    )
    validate_archive(archive_path, manifest)
    return VerifiedExport(export_id, manifest, archive_path)


def _output_overlaps_source(output: Path, source: Path) -> bool:
    output_resolved = output.resolve(strict=False)
    source_resolved = source.resolve(strict=False)
    return output_resolved == source_resolved or output_resolved in source_resolved.parents or source_resolved in output_resolved.parents


def _check_restore_output(output: Path, source_dir: Path) -> None:
    if _output_overlaps_source(output, source_dir):
        raise GalleryError("restore output must not overlap the live face-library path")
    if output.is_symlink():
        raise GalleryError("restore output must not be a symlink")
    if output.exists():
        if not output.is_dir():
            raise GalleryError("restore output must be a directory")
        try:
            next(output.iterdir())
        except StopIteration:
            return
        raise GalleryError("restore output is not empty; choose a new staging directory")


def extract_verified(verified: VerifiedExport, output: Path, source_dir: Path) -> int:
    """Extract a verified archive into an empty staging directory without overwrites."""
    _check_restore_output(output, source_dir)
    entries = _manifest_entries(verified.manifest)
    expected_names = {str(entry["path"]) for entry in entries}
    validate_archive(verified.archive_path, verified.manifest)
    output.mkdir(parents=True, exist_ok=True)
    output_root = output.resolve()
    created: list[Path] = []
    try:
        with zipfile.ZipFile(verified.archive_path, "r") as archive:
            for name in sorted(expected_names):
                relative = PurePosixPath(name)
                destination = output.joinpath(*relative.parts)
                if not destination.resolve(strict=False).is_relative_to(output_root):
                    raise GalleryError("archive member would escape the staging directory")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name, "r") as src, destination.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=_COPY_CHUNK)
                os.chmod(destination, 0o600)
                created.append(destination)
    except Exception:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return len(created)


def restore_remote(
    client,
    settings: Settings,
    export_id: str,
    output: Path,
    apply: bool,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="face-gallery-restore-") as directory:
        verified = verify_remote(client, settings, export_id, Path(directory))
        _check_restore_output(output, settings.source_dir)
        if apply:
            count = extract_verified(verified, output, settings.source_dir)
            LOG.info("Restored export_id=%s files=%d to staging output", export_id, count)
        else:
            LOG.info("DRY RUN restore export_id=%s verified; add --apply to extract", export_id)
        return verified.manifest


def run_export(settings: Settings, apply: bool) -> str:
    with tempfile.TemporaryDirectory(prefix="face-gallery-export-") as directory:
        bundle = build_export(settings, Path(directory))
        face_count = sum(1 for entry in bundle.manifest["files"] if entry["kind"] == "approved_face")  # type: ignore[index]
        if not apply:
            LOG.info("DRY RUN export_id=%s approved_images=%d; no remote writes", bundle.export_id, face_count)
            return bundle.export_id
        client = make_s3_client(settings)
        publish_export(client, settings, bundle)
        LOG.info("Published export_id=%s approved_images=%d", bundle.export_id, face_count)
        return bundle.export_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    export = subcommands.add_parser("export", help="build an export; upload only with --apply")
    export.add_argument("--apply", action="store_true", help="publish the export to S3")
    verify = subcommands.add_parser("verify", help="verify a completed remote export")
    verify.add_argument("export_id")
    restore = subcommands.add_parser("restore", help="verify and stage a remote export")
    restore.add_argument("export_id")
    restore.add_argument("--output", required=True, type=Path)
    restore.add_argument("--apply", action="store_true", help="extract into the empty staging output")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    try:
        settings = Settings.from_env()
        if args.command == "export":
            run_export(settings, args.apply)
        elif args.command == "verify":
            client = make_s3_client(settings)
            with tempfile.TemporaryDirectory(prefix="face-gallery-verify-") as directory:
                verify_remote(client, settings, args.export_id, Path(directory))
            LOG.info("Verified export_id=%s", args.export_id)
        elif args.command == "restore":
            client = make_s3_client(settings)
            restore_remote(client, settings, args.export_id, args.output, args.apply)
        return 0
    except (GalleryError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
