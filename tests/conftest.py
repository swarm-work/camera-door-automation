"""Shared fixtures.

The reconciler has exactly one injection seam: every function takes a frozen
``Settings``. These fixtures lean on that seam and on real SQLite files, so the
tests exercise the production SQL rather than a mock of it. S3 is faked with
moto (a real botocore stack against an in-process server), and Notion with
``responses``.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reconciler as rec  # noqa: E402

# Fixed instant so manifests and windows are deterministic.
NOW = 1_700_000_000.0


# --------------------------------------------------------------------------
# Fregata (source) database
# --------------------------------------------------------------------------

EVENT_DDL = """
CREATE TABLE {table} (
  id TEXT PRIMARY KEY,
  camera TEXT NOT NULL,
  label TEXT NOT NULL,
  sub_label TEXT,
  start_time REAL,
  end_time REAL,
  top_score REAL,
  false_positive INTEGER,
  zones TEXT,
  has_clip INTEGER,
  has_snapshot INTEGER,
  data TEXT
)
"""

RECORDING_DDL = """
CREATE TABLE {table} (
  id TEXT PRIMARY KEY,
  camera TEXT NOT NULL,
  path TEXT NOT NULL,
  start_time REAL NOT NULL,
  end_time REAL NOT NULL,
  duration REAL,
  objects INTEGER,
  motion INTEGER,
  regions INTEGER,
  segment_size REAL
)
"""

EVENT_FIELDS = (
    "id", "camera", "label", "sub_label", "start_time", "end_time",
    "top_score", "false_positive", "zones", "has_clip", "has_snapshot", "data",
)
RECORDING_FIELDS = (
    "id", "camera", "path", "start_time", "end_time",
    "duration", "objects", "motion", "regions", "segment_size",
)


def make_event(**over):
    row = {
        "id": "evt-1", "camera": "door_camera", "label": "person",
        "sub_label": None, "start_time": NOW, "end_time": NOW + 20.0,
        "top_score": 0.92, "false_positive": 0, "zones": '["porch"]',
        "has_clip": 1, "has_snapshot": 1, "data": '{"box": [1, 2, 3, 4]}',
    }
    row.update(over)
    return row


def make_recording(**over):
    row = {
        "id": "rec-1", "camera": "door_camera", "path": "/media/recordings/door_camera/seg-1.mp4",
        "start_time": NOW - 30.0, "end_time": NOW + 30.0,
        "duration": 60.0, "objects": 1, "motion": 5, "regions": 2, "segment_size": 1024.0,
    }
    row.update(over)
    return row


def build_source_db(
    path: Path,
    events=(),
    recordings=(),
    event_table: str = "event",
    recording_table: str = "recordings",
    extra_ddl: str | None = None,
) -> Path:
    """Write a Frigate-shaped SQLite file at ``path``."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(EVENT_DDL.format(table=f'"{event_table}"'))
        conn.execute(RECORDING_DDL.format(table=f'"{recording_table}"'))
        if extra_ddl:
            conn.executescript(extra_ddl)
        conn.executemany(
            f'INSERT INTO "{event_table}" ({",".join(EVENT_FIELDS)}) '
            f'VALUES ({",".join("?" * len(EVENT_FIELDS))})',
            [tuple(e[f] for f in EVENT_FIELDS) for e in events],
        )
        conn.executemany(
            f'INSERT INTO "{recording_table}" ({",".join(RECORDING_FIELDS)}) '
            f'VALUES ({",".join("?" * len(RECORDING_FIELDS))})',
            [tuple(r[f] for f in RECORDING_FIELDS) for r in recordings],
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def recordings_dir(tmp_path: Path) -> Path:
    d = tmp_path / "media" / "recordings"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def segment_file(recordings_dir: Path):
    """Create a settled (old enough) recording segment on disk."""
    def _make(rel: str = "door_camera/seg-1.mp4", body: bytes = b"\x00fake-mp4-bytes", age: float = 600.0):
        p = recordings_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        stamp = os.stat(p).st_mtime - age
        os.utime(p, (stamp, stamp))
        return p
    return _make


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@pytest.fixture
def settings_factory(tmp_path: Path, recordings_dir: Path):
    def _make(**over) -> rec.Settings:
        base = dict(
            source_db=tmp_path / "frigate.db",
            recordings_dir=recordings_dir,
            state_db=tmp_path / "state" / "reconciler-state.db",
            bucket="test-bucket",
            prefix="fregata",
            region="us-east-1",
            endpoint_url=None,
            camera="door_camera",
            label="person",
            pre_roll=10.0,
            post_roll=15.0,
            poll_seconds=30.0,
            settle_seconds=5.0,
            dry_run=False,
            upload_manifest=True,
            notion_token=None,
            notion_database_id=None,
            notion_version="2026-03-11",
            notion_include_person=True,
            notion_max_attempts=5,
            clip_links=False,
            clip_url_ttl=604800,
            clip_refresh_seconds=432000,
            clip_aws_access_key_id=None,
            clip_aws_secret_access_key=None,
            slack_webhook_url=None,
            slack_summary_hour=21,
            slack_summary_minute=0,
            slack_summary_on_empty=False,
            slack_include_known=False,
            slack_include_snapshots=False,
            slack_unknown_requires_face=False,
        )
        base.update(over)
        return rec.Settings(**base)
    return _make


@pytest.fixture
def state_db(settings_factory):
    conn = rec.open_state(settings_factory().state_db)
    yield conn
    conn.close()


# --------------------------------------------------------------------------
# S3 (moto)
# --------------------------------------------------------------------------

BUCKET = "test-bucket"

FAKE_AWS_ENV = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
}


@pytest.fixture(scope="session", autouse=True)
def _fake_aws_env():
    """Guarantee boto never reaches for real credentials, even by accident."""
    saved = {k: os.environ.get(k) for k in FAKE_AWS_ENV}
    os.environ.update(FAKE_AWS_ENV)
    yield
    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@pytest.fixture
def aws_credentials(_fake_aws_env):
    """Function-scoped alias, for tests that only need the environment."""


@pytest.fixture(scope="session")
def _moto(_fake_aws_env):
    from moto import mock_aws

    with mock_aws():
        yield


@pytest.fixture(scope="session")
def _s3_client(_moto):
    """One client for the whole session.

    Constructing a boto3 client loads botocore's service model from disk, which
    costs ~5s per call when the checkout lives on a Windows drive mounted into
    WSL. Paying it once takes the suite from minutes to seconds.
    """
    from pathlib import Path as _P
    return rec.s3_client(rec.Settings(
        source_db=_P("."), recordings_dir=_P("."), state_db=_P("."), bucket=BUCKET,
        prefix="", region="us-east-1", endpoint_url=None, camera=None, label="person",
        pre_roll=0, post_roll=0, poll_seconds=0, settle_seconds=0, dry_run=True,
        upload_manifest=False, notion_token=None, notion_database_id=None,
        notion_version="v", notion_include_person=False, notion_max_attempts=5,
        clip_links=False, clip_url_ttl=604800, clip_refresh_seconds=432000,
        clip_aws_access_key_id=None, clip_aws_secret_access_key=None,
        slack_webhook_url=None, slack_summary_hour=21, slack_summary_minute=0,
        slack_summary_on_empty=False, slack_include_known=False,
        slack_include_snapshots=False, slack_unknown_requires_face=False))


@pytest.fixture
def s3(_s3_client):
    """A moto-backed S3 client with an empty target bucket."""
    _empty_bucket(_s3_client, BUCKET)
    _s3_client.create_bucket(Bucket=BUCKET)
    yield _s3_client
    _empty_bucket(_s3_client, BUCKET)


def _empty_bucket(client, bucket: str) -> None:
    from botocore.exceptions import ClientError
    try:
        while True:
            page = client.list_objects_v2(Bucket=bucket)
            contents = page.get("Contents") or []
            if not contents:
                break
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": o["Key"]} for o in contents]})
        client.delete_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"NoSuchBucket", "404"}:
            raise


# --------------------------------------------------------------------------
# A whole wired-up pipeline: source db + state db + s3 + settings
# --------------------------------------------------------------------------

@pytest.fixture
def pipeline(tmp_path, settings_factory, s3, segment_file):
    """Build the argument bundle ``process_event`` demands."""
    class Wired:
        def __init__(self, settings, fconn, state, client, snapshot):
            self.settings = settings
            self.fconn = fconn
            self.state = state
            self.client = client
            self.snapshot = snapshot
            self.recording_table, self.recording_cols = rec.resolve_table(
                fconn, ("recordings", "recording"), rec.RECORDING_REQUIRED)
            self.event_table, self.event_cols = rec.resolve_table(
                fconn, ("event", "events"), rec.EVENT_REQUIRED)

        def events(self):
            return rec.fetch_events(self.fconn, self.event_table, self.event_cols, self.settings)

        def run(self, event):
            rec.process_event(event, self.fconn, self.recording_table,
                              self.recording_cols, self.state, self.client, self.settings)

        def keys(self):
            resp = self.client.list_objects_v2(Bucket=self.settings.bucket)
            return sorted(o["Key"] for o in resp.get("Contents", []))

        def close(self):
            self.fconn.close()
            self.state.close()

    made = []

    def _build(events=(), recordings=(), **setting_over):
        # Each build gets its own source DB so a test can wire up the same data
        # twice (e.g. a dry-run pass followed by a live one).
        setting_over.setdefault("source_db", tmp_path / f"frigate-{len(made)}.db")
        settings = settings_factory(**setting_over)
        build_source_db(settings.source_db, events=events, recordings=recordings)
        snapshot = rec.snapshot_db(settings.source_db)
        fconn = sqlite3.connect(snapshot)
        fconn.row_factory = sqlite3.Row
        state = rec.open_state(settings.state_db)
        w = Wired(settings, fconn, state, s3, snapshot)
        made.append(w)
        return w

    yield _build

    for w in made:
        w.close()
        w.snapshot.unlink(missing_ok=True)
