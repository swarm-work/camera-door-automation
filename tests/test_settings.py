"""Configuration parsing — the layer where a typo becomes a production incident."""
from __future__ import annotations

from pathlib import Path

import pytest

import reconciler as rec


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """``from_env`` calls ``load_dotenv()``, which reads whatever .env happens to
    be on disk. Neutralise it so these tests describe the environment only."""
    monkeypatch.setattr(rec, "load_dotenv", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "FREGATA_DB_PATH", "FREGATA_RECORDINGS_DIR", "STATE_DB_PATH", "S3_BUCKET",
        "S3_PREFIX", "AWS_REGION", "S3_ENDPOINT_URL", "CAMERA", "LABEL",
        "PRE_ROLL_SECONDS", "POST_ROLL_SECONDS", "POLL_SECONDS", "SETTLE_SECONDS",
        "DRY_RUN", "UPLOAD_EVENT_MANIFEST", "NOTION_TOKEN", "NOTION_DATABASE_ID",
        "NOTION_VERSION", "CLIP_LINKS", "CLIP_URL_TTL_SECONDS", "CLIP_REFRESH_SECONDS",
        "CLIP_AWS_ACCESS_KEY_ID", "CLIP_AWS_SECRET_ACCESS_KEY",
        "SLACK_WEBHOOK_URL", "SLACK_SUMMARY_TIME", "SLACK_SUMMARY_ON_EMPTY",
        "SLACK_INCLUDE_KNOWN", "SLACK_INCLUDE_SNAPSHOTS",
        "SLACK_UNKNOWN_REQUIRES_FACE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_safe(monkeypatch):
    """An empty environment must not upload anything anywhere."""
    s = rec.Settings.from_env()
    assert s.dry_run is True, "an unconfigured install must never write to S3"
    assert s.bucket == ""
    assert s.camera is None
    assert s.label == "person"
    assert s.prefix == "fregata"
    assert s.region == "us-east-1"
    assert s.endpoint_url is None
    assert (s.pre_roll, s.post_roll) == (10.0, 15.0)
    assert (s.poll_seconds, s.settle_seconds) == (30.0, 5.0)
    assert s.notion_token is None and s.notion_database_id is None
    assert s.clip_links is False, "clip links must be opt-in"
    assert s.slack_unknown_requires_face is False


def test_manifest_upload_defaults_off(monkeypatch):
    """The manifest embeds sub_label — a real name. Deleting the env line, which
    .env.example's own header recommends for unwanted settings, must not enable it."""
    assert rec.Settings.from_env().upload_manifest is False

def test_unknown_face_filter_is_opt_in(monkeypatch):
    monkeypatch.setenv("SLACK_UNKNOWN_REQUIRES_FACE", "true")
    assert rec.Settings.from_env().slack_unknown_requires_face is True


def test_clip_ttl_is_clamped_to_the_sigv4_maximum(monkeypatch):
    monkeypatch.setenv("CLIP_URL_TTL_SECONDS", "999999999")
    assert rec.Settings.from_env().clip_url_ttl == 604_800


def test_clip_refresh_must_be_shorter_than_the_ttl(monkeypatch):
    """A refresh age at or past the TTL guarantees every link dies before renewal."""
    monkeypatch.setenv("CLIP_LINKS", "true")
    monkeypatch.setenv("CLIP_URL_TTL_SECONDS", "86400")
    monkeypatch.setenv("CLIP_REFRESH_SECONDS", "86400")
    with pytest.raises(ValueError):
        rec.Settings.from_env()


def test_clip_refresh_is_not_validated_when_the_feature_is_off(monkeypatch):
    """A stale pair of CLIP_* leftovers must not stop the reconciler itself."""
    monkeypatch.setenv("CLIP_URL_TTL_SECONDS", "86400")
    monkeypatch.setenv("CLIP_REFRESH_SECONDS", "86400")
    assert rec.Settings.from_env().clip_links is False


def test_blank_clip_signer_credentials_become_none(monkeypatch):
    monkeypatch.setenv("CLIP_AWS_ACCESS_KEY_ID", "   ")
    monkeypatch.setenv("CLIP_AWS_SECRET_ACCESS_KEY", "")
    s = rec.Settings.from_env()
    assert s.clip_aws_access_key_id is None and s.clip_aws_secret_access_key is None


@pytest.mark.parametrize("var", ["CLIP_AWS_ACCESS_KEY_ID", "CLIP_AWS_SECRET_ACCESS_KEY"])
def test_a_half_configured_clip_signer_is_rejected(monkeypatch, var):
    """One of the pair alone would silently sign with the main upload credentials —
    and deactivating the dedicated key would then revoke nothing."""
    monkeypatch.setenv(var, "value")
    with pytest.raises(ValueError):
        rec.Settings.from_env()


def test_settings_is_immutable():
    s = rec.Settings.from_env()
    with pytest.raises(Exception):
        s.dry_run = False  # type: ignore[misc]


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "  True  ", "yes", "on", "ON"])
def test_truthy_bool_spellings(monkeypatch, raw):
    monkeypatch.setenv("DRY_RUN", raw)
    assert rec.Settings.from_env().dry_run is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  ", "nope", "2", "y", "t"])
def test_everything_else_is_false(monkeypatch, raw):
    """Documented in .env.example: only 1/true/yes/on are true.

    Note the consequence — ``DRY_RUN=y`` and ``DRY_RUN=`` both mean LIVE.
    """
    monkeypatch.setenv("DRY_RUN", raw)
    assert rec.Settings.from_env().dry_run is False


def test_empty_dry_run_flips_to_live(monkeypatch):
    """The sharp edge, pinned on its own: blanking the var disables dry run."""
    monkeypatch.setenv("DRY_RUN", "")
    assert rec.Settings.from_env().dry_run is False


def test_paths_are_tilde_expanded(monkeypatch):
    monkeypatch.setenv("FREGATA_DB_PATH", "~/Fregata/config/frigate.db")
    monkeypatch.setenv("FREGATA_RECORDINGS_DIR", "~/Fregata/media/recordings")
    monkeypatch.setenv("STATE_DB_PATH", "~/state.db")
    s = rec.Settings.from_env()
    home = Path.home()
    assert s.source_db == home / "Fregata/config/frigate.db"
    assert s.recordings_dir == home / "Fregata/media/recordings"
    assert s.state_db == home / "state.db"


@pytest.mark.parametrize("raw,expected", [
    ("fregata", "fregata"),
    ("/fregata/", "fregata"),
    ("///a/b///", "a/b"),
    ("", ""),
])
def test_prefix_slashes_are_stripped(monkeypatch, raw, expected):
    monkeypatch.setenv("S3_PREFIX", raw)
    assert rec.Settings.from_env().prefix == expected


def test_blank_camera_means_all_cameras(monkeypatch):
    monkeypatch.setenv("CAMERA", "   ")
    assert rec.Settings.from_env().camera is None


def test_blank_bucket_is_stripped_not_none(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "  my-bucket  ")
    assert rec.Settings.from_env().bucket == "my-bucket"


def test_blank_endpoint_url_becomes_none(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    assert rec.Settings.from_env().endpoint_url is None


def test_notion_credentials_are_stripped_to_none(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "   ")
    monkeypatch.setenv("NOTION_DATABASE_ID", "")
    s = rec.Settings.from_env()
    assert s.notion_token is None and s.notion_database_id is None


@pytest.mark.parametrize("var", [
    "PRE_ROLL_SECONDS", "POST_ROLL_SECONDS", "POLL_SECONDS", "SETTLE_SECONDS",
])
def test_non_numeric_duration_crashes_at_startup(monkeypatch, var):
    """A unit suffix is a startup crash, as .env.example warns."""
    monkeypatch.setenv(var, "10s")
    with pytest.raises(ValueError):
        rec.Settings.from_env()


@pytest.mark.parametrize("var", [
    "PRE_ROLL_SECONDS", "POST_ROLL_SECONDS", "POLL_SECONDS", "SETTLE_SECONDS",
])
def test_blank_duration_crashes_at_startup(monkeypatch, var):
    """Blanking rather than deleting a numeric var takes the service down."""
    monkeypatch.setenv(var, "")
    with pytest.raises(ValueError):
        rec.Settings.from_env()


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "No validation: a negative PRE_ROLL_SECONDS silently inverts the archive "
    "window, so segments before the event are excluded instead of included."))
def test_negative_padding_is_rejected(monkeypatch):
    monkeypatch.setenv("PRE_ROLL_SECONDS", "-30")
    with pytest.raises(ValueError):
        rec.Settings.from_env()


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "No validation: DRY_RUN=false with an empty S3_BUCKET is accepted at "
    "startup and only fails later, per-event, inside upload_file."))
def test_live_mode_without_bucket_is_rejected_at_startup(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("S3_BUCKET", "")
    with pytest.raises(Exception):
        rec.Settings.from_env()


def test_slack_defaults(monkeypatch):
    s = rec.Settings.from_env()
    assert s.slack_webhook_url is None
    assert (s.slack_summary_hour, s.slack_summary_minute) == (21, 0)
    assert s.slack_summary_on_empty is False


def test_slack_summary_time_parses(monkeypatch):
    monkeypatch.setenv("SLACK_SUMMARY_TIME", "07:30")
    s = rec.Settings.from_env()
    assert (s.slack_summary_hour, s.slack_summary_minute) == (7, 30)


@pytest.mark.parametrize("bad", ["evening", "25:00", "21:60", "21", "21:00:00", ""])
def test_bad_slack_summary_time_crashes_at_startup(monkeypatch, bad):
    """A typo'd time must fail loudly, not silently never send a summary."""
    monkeypatch.setenv("SLACK_SUMMARY_TIME", bad)
    with pytest.raises(ValueError):
        rec.Settings.from_env()
