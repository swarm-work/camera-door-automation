"""Behavioral contract for the private approved-face export pipeline."""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

import face_gallery as gallery


@pytest.fixture
def face_source(tmp_path: Path) -> Path:
    source = tmp_path / "media" / "clips" / "faces"
    source.mkdir(parents=True)
    return source


@pytest.fixture
def gallery_settings(face_source: Path) -> gallery.Settings:
    return gallery.Settings(
        source_dir=face_source,
        bucket="test-bucket",
        prefix="face-gallery/v1",
        kms_key_id=None,
        aws_profile=None,
        bfr_db=None,
        region="us-east-1",
        endpoint_url=None,
    )


def add_face(source: Path, person: str = "Private Person", name: str = "face.jpg", body: bytes = b"jpeg") -> Path:
    path = source / person / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def publish_sample(s3, settings: gallery.Settings, tmp_path: Path) -> gallery.ExportBundle:
    add_face(settings.source_dir)
    bundle = gallery.build_export(settings, tmp_path / "build")
    gallery.publish_export(s3, settings, bundle)
    return bundle


def encrypted_put(s3, settings: gallery.Settings, key: str, body: bytes) -> None:
    s3.put_object(
        Bucket=settings.bucket,
        Key=key,
        Body=body,
        Metadata={"sha256": hashlib.sha256(body).hexdigest()},
        **gallery.encryption_args(settings),
    )


def test_settings_derive_face_source_and_fallbacks(tmp_path: Path):
    recordings = tmp_path / "media" / "recordings"
    settings = gallery.Settings.from_env({
        "FREGATA_RECORDINGS_DIR": str(recordings),
        "S3_BUCKET": "fallback-bucket",
        "AWS_PROFILE": "backup-profile",
    })

    assert settings.source_dir == tmp_path / "media" / "clips" / "faces"
    assert settings.bucket == "fallback-bucket"
    assert settings.prefix == "face-gallery/v1"
    assert settings.aws_profile == "backup-profile"


def test_selection_is_approved_images_only(face_source: Path, tmp_path: Path):
    approved_jpg = add_face(face_source, "Person A", "one.JPG")
    approved_nested = add_face(face_source, "Person A", "angles/two.webp")
    add_face(face_source, "Person A", "notes.txt")
    add_face(face_source, "Person A", ".hidden.jpg")
    add_face(face_source, "Person A", ".hidden-dir/face.png")
    add_face(face_source, "Person A", "train/queued.jpg")
    add_face(face_source, "train", "unreviewed.jpg")
    add_face(face_source, ".private", "hidden-person.jpg")
    (face_source / "loose.jpg").write_bytes(b"not in a person directory")

    external = tmp_path / "outside.jpg"
    external.write_bytes(b"outside")
    (face_source / "Person A" / "linked.jpg").symlink_to(external)
    external_dir = tmp_path / "outside-dir"
    external_dir.mkdir()
    (external_dir / "face.png").write_bytes(b"outside")
    (face_source / "Person A" / "linked-dir").symlink_to(external_dir, target_is_directory=True)

    selected = gallery.collect_approved_images(face_source)

    assert [item.source for item in selected] == [approved_nested, approved_jpg]
    assert [item.archive_path for item in selected] == [
        "faces/Person A/angles/two.webp",
        "faces/Person A/one.JPG",
    ]


def test_export_id_and_archive_are_deterministic(gallery_settings: gallery.Settings, tmp_path: Path):
    first = add_face(gallery_settings.source_dir, "Person B", "b.png", b"second")
    second = add_face(gallery_settings.source_dir, "Person A", "a.jpeg", b"first")
    os.utime(first, (1_600_000_000, 1_600_000_000))
    os.utime(second, (1_700_000_000, 1_700_000_000))

    one = gallery.build_export(gallery_settings, tmp_path / "one")
    os.utime(first, (1_800_000_000, 1_800_000_000))
    os.utime(second, (1_900_000_000, 1_900_000_000))
    two = gallery.build_export(gallery_settings, tmp_path / "two")

    assert one.export_id == two.export_id
    assert one.manifest_bytes == two.manifest_bytes
    assert one.archive_path.read_bytes() == two.archive_path.read_bytes()
    assert len(one.export_id) == 64


def test_export_command_is_dry_run_and_logs_no_person_name(
    gallery_settings: gallery.Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    add_face(gallery_settings.source_dir, "Sensitive Name")
    monkeypatch.setattr(gallery.Settings, "from_env", classmethod(lambda cls: gallery_settings))
    monkeypatch.setattr(
        gallery,
        "make_s3_client",
        lambda settings: pytest.fail("dry-run export constructed an S3 client"),
    )

    with caplog.at_level(logging.INFO, logger="face_gallery"):
        result = gallery.main(["export"])

    assert result == 0
    assert "DRY RUN" in caplog.text
    assert "Sensitive Name" not in caplog.text


def test_publish_encrypts_every_object_and_writes_complete_last(
    s3,
    gallery_settings: gallery.Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    person = "Sensitive Name"
    add_face(gallery_settings.source_dir, person)
    bundle = gallery.build_export(gallery_settings, tmp_path / "build")
    writes: list[tuple[str, str, dict[str, object]]] = []
    original_upload = s3.upload_file
    original_put = s3.put_object

    def recording_upload(filename, bucket, key, ExtraArgs=None, **kwargs):
        writes.append(("upload", key, dict(ExtraArgs or {})))
        return original_upload(filename, bucket, key, ExtraArgs=ExtraArgs, **kwargs)

    def recording_put(**kwargs):
        writes.append(("put", kwargs["Key"], dict(kwargs)))
        return original_put(**kwargs)

    monkeypatch.setattr(s3, "upload_file", recording_upload)
    monkeypatch.setattr(s3, "put_object", recording_put)
    gallery.publish_export(s3, gallery_settings, bundle)

    publication_writes = [write for write in writes if write[1].endswith(
        (gallery.ARCHIVE_NAME, gallery.MANIFEST_NAME, gallery.COMPLETE_NAME)
    )]
    assert publication_writes[-1][1].endswith("/COMPLETE")
    assert all(person not in key for _, key, _ in publication_writes)
    for _, key, arguments in publication_writes:
        encryption = arguments.get("ServerSideEncryption")
        if encryption is None:
            encryption = arguments.get("ExtraArgs", {}).get("ServerSideEncryption")
        assert encryption == "AES256", key

    keys = gallery.export_keys(gallery_settings, bundle.export_id)
    assert s3.head_object(Bucket=gallery_settings.bucket, Key=keys["archive"])["ServerSideEncryption"] == "AES256"
    assert s3.head_object(Bucket=gallery_settings.bucket, Key=keys["manifest"])["ServerSideEncryption"] == "AES256"
    assert s3.head_object(Bucket=gallery_settings.bucket, Key=keys["complete"])["ServerSideEncryption"] == "AES256"


def test_kms_configuration_is_explicit(gallery_settings: gallery.Settings):
    settings = replace(gallery_settings, kms_key_id="arn:aws:kms:us-east-1:123456789012:key/example")
    assert gallery.encryption_args(settings) == {
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": "arn:aws:kms:us-east-1:123456789012:key/example",
    }


def test_verify_rejects_missing_export(s3, gallery_settings: gallery.Settings, tmp_path: Path):
    with pytest.raises(gallery.GalleryError, match="missing or unreadable"):
        gallery.verify_remote(s3, gallery_settings, "0" * 64, tmp_path / "verify")


def test_verify_rejects_export_without_complete_marker(s3, gallery_settings: gallery.Settings, tmp_path: Path):
    bundle = publish_sample(s3, gallery_settings, tmp_path)
    keys = gallery.export_keys(gallery_settings, bundle.export_id)
    s3.delete_object(Bucket=gallery_settings.bucket, Key=keys["complete"])

    with pytest.raises(gallery.GalleryError, match="missing or unreadable"):
        gallery.verify_remote(s3, gallery_settings, bundle.export_id, tmp_path / "verify")


@pytest.mark.parametrize("object_name", ["archive", "manifest"])
def test_verify_rejects_tampered_export(
    s3,
    gallery_settings: gallery.Settings,
    tmp_path: Path,
    object_name: str,
):
    bundle = publish_sample(s3, gallery_settings, tmp_path)
    key = gallery.export_keys(gallery_settings, bundle.export_id)[object_name]
    encrypted_put(s3, gallery_settings, key, b"tampered")

    with pytest.raises(gallery.GalleryError, match="SHA-256|size"):
        gallery.verify_remote(s3, gallery_settings, bundle.export_id, tmp_path / "verify")


def test_restore_verifies_then_extracts_only_to_empty_staging(
    s3,
    gallery_settings: gallery.Settings,
    tmp_path: Path,
):
    bundle = publish_sample(s3, gallery_settings, tmp_path)
    output = tmp_path / "restore-staging"

    gallery.restore_remote(s3, gallery_settings, bundle.export_id, output, apply=False)
    assert not output.exists()

    gallery.restore_remote(s3, gallery_settings, bundle.export_id, output, apply=True)
    assert (output / "faces" / "Private Person" / "face.jpg").read_bytes() == b"jpeg"

    with pytest.raises(gallery.GalleryError, match="not empty"):
        gallery.restore_remote(s3, gallery_settings, bundle.export_id, output, apply=True)


def test_restore_refuses_live_source_path_after_remote_verification(
    s3,
    gallery_settings: gallery.Settings,
    tmp_path: Path,
):
    bundle = publish_sample(s3, gallery_settings, tmp_path)
    with pytest.raises(gallery.GalleryError, match="live face-library"):
        gallery.restore_remote(
            s3,
            gallery_settings,
            bundle.export_id,
            gallery_settings.source_dir,
            apply=True,
        )


def test_restore_rejects_archive_traversal(tmp_path: Path, gallery_settings: gallery.Settings):
    body = b"escape"
    entry = {
        "kind": "approved_face",
        "path": "../escape.jpg",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
    }
    archive_path = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.jpg", body)
    manifest = {"schema_version": gallery.SCHEMA_VERSION, "files": [entry]}
    verified = gallery.VerifiedExport("0" * 64, manifest, archive_path)
    output = tmp_path / "staging"

    with pytest.raises(gallery.GalleryError, match="unsafe archive path"):
        gallery.extract_verified(verified, output, gallery_settings.source_dir)
    assert not (tmp_path / "escape.jpg").exists()


def test_optional_sqlite_backup_is_consistent_and_restorable(
    gallery_settings: gallery.Settings,
    tmp_path: Path,
):
    add_face(gallery_settings.source_dir)
    database = tmp_path / "bfr.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO embeddings(value) VALUES ('saved')")
        connection.commit()

    bundle = gallery.build_export(replace(gallery_settings, bfr_db=database), tmp_path / "build")
    restored_db = tmp_path / "restored-bfr.db"
    with zipfile.ZipFile(bundle.archive_path) as archive:
        restored_db.write_bytes(archive.read("database/bfr.db"))
    with sqlite3.connect(restored_db) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM embeddings").fetchone() == ("saved",)
    database_entries = [entry for entry in bundle.manifest["files"] if entry["kind"] == "bfr_database"]
    assert len(database_entries) == 1
    assert database_entries[0]["path"] == "database/bfr.db"
