"""One entry-focused MP4 per Frigate event, plus resumable historical backfill."""
from __future__ import annotations

import json
import tempfile

import pytest
import requests
import responses

import reconciler as rec
from conftest import NOW, build_source_db, make_event

API = "http://frigate.test/api"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"event-data"


@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


def seed_completed(state, event_id: str = "e1", *, page_id: str | None = "p1") -> None:
    state.execute(
        "INSERT INTO event_delivery(event_id,camera,start_time,end_time,completed_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (event_id, "door_camera", NOW, NOW + 20.0, NOW, NOW),
    )
    if page_id is not None:
        state.execute(
            "INSERT INTO notion_delivery(event_id,page_id,synced_at,clip_signed_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (event_id, page_id, NOW, NOW, NOW),
        )
    state.commit()


def test_entry_window_is_anchored_to_detection_start(settings_factory):
    settings = settings_factory(event_clip_pre_roll=3.0, event_clip_duration=15.0)
    start, end = rec.event_clip_window(
        make_event(start_time=NOW, end_time=NOW + 90.0), settings)
    assert start == NOW - 3.0
    assert end == NOW + 15.0


def test_event_clip_upload_is_streamed_recorded_and_idempotent(
        state_db, settings_factory, s3, http, monkeypatch, tmp_path):
    settings = settings_factory(
        clip_source="frigate_api", upload_manifest=False,
        frigate_api_url=API,
    )
    event = make_event(id="e1", start_time=NOW, end_time=NOW + 60.0)
    url = rec.frigate_event_clip_url(settings, "door_camera", NOW - 3, NOW + 15)
    http.add(responses.GET, url, body=MP4, status=200,
             content_type="video/mp4")
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))

    first = rec.deliver_event_clip(event, state_db, s3, settings)
    second = rec.deliver_event_clip(event, state_db, s3, settings)

    assert first == second
    assert first["s3_key"] == "fregata/events/door_camera/e1/clip.mp4"
    assert first["size_bytes"] == len(MP4)
    body = s3.get_object(Bucket="test-bucket", Key=first["s3_key"])["Body"].read()
    assert body == MP4
    row = state_db.execute(
        "SELECT * FROM event_clip_delivery WHERE event_id='e1'").fetchone()
    assert row["uploaded_at"] is not None
    assert row["last_error"] is None
    assert row["size_bytes"] == len(body)
    assert list(spool.iterdir()) == []


def test_event_clip_http_failure_is_retryable_and_cleans_temp(
        state_db, settings_factory, s3, http, monkeypatch, tmp_path):
    settings = settings_factory(
        clip_source="frigate_api", upload_manifest=False,
        frigate_api_url=API,
    )
    event = make_event(id="e1")
    url = rec.frigate_event_clip_url(settings, "door_camera", NOW - 3, NOW + 15)
    http.add(responses.GET, url, body="gone", status=500)
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))

    with pytest.raises(requests.HTTPError):
        rec.deliver_event_clip(event, state_db, s3, settings)

    row = state_db.execute(
        "SELECT * FROM event_clip_delivery WHERE event_id='e1'").fetchone()
    assert row["uploaded_at"] is None
    assert "500" in row["last_error"]
    assert list(spool.iterdir()) == []


def test_empty_event_clip_never_marks_delivery_complete(
        state_db, settings_factory, s3, http):
    settings = settings_factory(
        clip_source="frigate_api", upload_manifest=False,
        frigate_api_url=API,
    )
    event = make_event(id="e1")
    url = rec.frigate_event_clip_url(settings, "door_camera", NOW - 3, NOW + 15)
    http.add(responses.GET, url, body=b"", status=200,
             content_type="video/mp4")

    with pytest.raises(RuntimeError, match="empty entry clip"):
        rec.deliver_event_clip(event, state_db, s3, settings)
    assert state_db.execute(
        "SELECT uploaded_at FROM event_clip_delivery WHERE event_id='e1'"
    ).fetchone()[0] is None


def test_non_mp4_response_is_rejected_before_upload(
        state_db, settings_factory, s3, http):
    settings = settings_factory(
        clip_source="frigate_api", upload_manifest=False,
        frigate_api_url=API,
    )
    event = make_event(id="e1")
    url = rec.frigate_event_clip_url(settings, "door_camera", NOW - 3, NOW + 15)
    http.add(responses.GET, url, body=b"<html>not a clip</html>", status=200,
             content_type="text/html")

    with pytest.raises(RuntimeError, match="not an MP4"):
        rec.deliver_event_clip(event, state_db, s3, settings)
    assert s3.list_objects_v2(Bucket="test-bucket").get("KeyCount") == 0


def test_process_event_api_mode_uploads_one_clip_not_raw_segments(
        pipeline, s3, http):
    settings_url = API
    url = f"{API}/door_camera/start/1699999997.000000/end/1700000015.000000/clip.mp4"
    http.add(responses.GET, url, body=MP4, status=200,
             content_type="video/mp4")
    w = pipeline(
        events=[make_event(id="e1", start_time=NOW, end_time=NOW + 90.0)],
        recordings=[], clip_source="frigate_api", frigate_api_url=settings_url,
    )

    w.run(w.events()[0])

    assert w.keys() == [
        "fregata/events/door_camera/e1/clip.mp4",
        "fregata/events/door_camera/e1/manifest.json",
    ]
    assert w.state.execute(
        "SELECT COUNT(*) FROM segment_delivery").fetchone()[0] == 0
    delivered = w.state.execute(
        "SELECT * FROM event_clip_delivery WHERE event_id='e1'").fetchone()
    assert delivered["uploaded_at"] is not None
    manifest = json.loads(s3.get_object(
        Bucket="test-bucket",
        Key="fregata/events/door_camera/e1/manifest.json")["Body"].read())
    assert manifest["schema_version"] == 2
    assert manifest["archive_window"]["mode"] == "entry"
    assert manifest["segments"] == []
    assert manifest["clip"]["s3_key"].endswith("/clip.mp4")


def test_clip_refresh_prefers_generated_event_mp4(
        state_db, settings_factory, s3, monkeypatch):
    seed_completed(state_db)
    key = "fregata/events/door_camera/e1/clip.mp4"
    s3.put_object(Bucket="test-bucket", Key=key, Body=b"mp4")
    state_db.execute(
        """INSERT INTO event_clip_delivery(
             event_id,bucket,endpoint_url,region,source,camera,start_time,end_time,
             s3_key,etag,size_bytes,generated_at,uploaded_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("e1", "test-bucket", "", "us-east-1", "frigate_api", "door_camera",
         NOW - 3, NOW + 15, key, "etag", 3, NOW, NOW, NOW),
    )
    state_db.commit()
    settings = settings_factory(
        clip_links=True, notion_token="token", notion_database_id="db")
    patched = []

    def notion(method, path, _settings, payload=None):
        if method == "PATCH":
            patched.append(payload)
        return {"id": "p1"}

    monkeypatch.setattr(rec, "notion_request", notion)
    monkeypatch.setattr(rec, "warn_if_presign_capped", lambda _settings: None)
    monkeypatch.setattr(rec, "verify_clip_url", lambda *args: None)

    rec.refresh_clip_links(state_db, s3, settings)

    url = patched[0]["properties"]["Clip"]["url"]
    assert "/events/door_camera/e1/clip.mp4" in url
    assert "index.html" not in url
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="test-bucket").get("Contents", [])]
    assert not any(k.endswith("index.html") for k in keys)


def test_backfill_dry_run_reports_without_network_or_state_write(
        settings_factory, capsys):
    settings = settings_factory(clip_source="frigate_api", dry_run=False)
    start, _, _ = rec.parse_date_window("2026-08-19")
    build_source_db(settings.source_db, events=[
        make_event(id="e1", start_time=start + 60, end_time=start + 120),
    ])
    state = rec.open_state(settings.state_db)
    seed_completed(state)
    state.close()

    assert rec.event_clips_backfill(settings, "2026-08-19", apply=False) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["would_generate"] == 1
    state = rec.open_state(settings.state_db)
    try:
        assert state.execute("SELECT COUNT(*) FROM event_clip_delivery").fetchone()[0] == 0
    finally:
        state.close()


def test_backfill_apply_uploads_and_marks_notion_link_stale(
        settings_factory, s3, http, capsys):
    settings = settings_factory(
        clip_source="frigate_api", dry_run=False, frigate_api_url=API)
    start, _, _ = rec.parse_date_window("2026-08-19")
    event = make_event(
        id="e1", start_time=start + 60, end_time=start + 120)
    build_source_db(settings.source_db, events=[event])
    state = rec.open_state(settings.state_db)
    seed_completed(state)
    state.close()
    clip_start, clip_end = rec.event_clip_window(event, settings)
    url = rec.frigate_event_clip_url(
        settings, "door_camera", clip_start, clip_end)
    http.add(responses.GET, url, body=MP4, status=200,
             content_type="video/mp4")

    assert rec.event_clips_backfill(
        settings, "2026-08-19", apply=True) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["generated"] == 1
    state = rec.open_state(settings.state_db)
    try:
        assert state.execute(
            "SELECT uploaded_at FROM event_clip_delivery WHERE event_id='e1'"
        ).fetchone()[0] is not None
        assert state.execute(
            "SELECT clip_signed_at FROM notion_delivery WHERE event_id='e1'"
        ).fetchone()[0] is None
    finally:
        state.close()
    assert s3.get_object(
        Bucket="test-bucket",
        Key="fregata/events/door_camera/e1/clip.mp4")["Body"].read() == MP4


def test_backfill_missing_source_keeps_legacy_link(
        settings_factory, s3, http, capsys):
    settings = settings_factory(
        clip_source="frigate_api", dry_run=False, frigate_api_url=API)
    start, _, _ = rec.parse_date_window("2026-08-19")
    event = make_event(
        id="e1", start_time=start + 60, end_time=start + 120)
    build_source_db(settings.source_db, events=[event])
    state = rec.open_state(settings.state_db)
    seed_completed(state)
    state.close()
    clip_start, clip_end = rec.event_clip_window(event, settings)
    url = rec.frigate_event_clip_url(
        settings, "door_camera", clip_start, clip_end)
    http.add(responses.GET, url, body="recording gone", status=404)

    assert rec.event_clips_backfill(
        settings, "2026-08-19", apply=True) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["source_unavailable"] == 1
    state = rec.open_state(settings.state_db)
    try:
        assert state.execute(
            "SELECT clip_signed_at FROM notion_delivery WHERE event_id='e1'"
        ).fetchone()[0] == NOW
    finally:
        state.close()


def test_old_state_database_gains_event_clip_table(settings_factory):
    state = rec.open_state(settings_factory().state_db)
    try:
        assert {
            "event_id", "bucket", "endpoint_url", "source", "s3_key",
            "size_bytes", "uploaded_at", "last_error",
        } <= rec.table_columns(state, "event_clip_delivery")
    finally:
        state.close()
