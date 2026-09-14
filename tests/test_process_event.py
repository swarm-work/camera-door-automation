"""End-to-end delivery of a single event: source DB -> disk -> S3 -> state.

This is the function that carries the program's correctness. Everything else is
plumbing around it.
"""
from __future__ import annotations

import json
import os

import pytest

import reconciler as rec
from conftest import NOW, make_event, make_recording


def seg_row(rel: str, **over):
    """A recordings row pointing at ``rel`` under the host media root."""
    row = dict(id=rel, camera="door_camera", path=f"/media/frigate/recordings/{rel}",
               start_time=NOW - 30.0, end_time=NOW + 30.0)
    row.update(over)
    return make_recording(**row)


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------

def test_delivers_segments_and_manifest(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    segment_file("door_camera/seg-2.mp4")
    w = pipeline(
        events=[make_event(id="e1")],
        recordings=[seg_row("door_camera/seg-1.mp4", start_time=NOW - 30, end_time=NOW),
                    seg_row("door_camera/seg-2.mp4", start_time=NOW, end_time=NOW + 30)],
    )
    w.run(w.events()[0])

    assert w.keys() == [
        "fregata/events/door_camera/e1/manifest.json",
        "fregata/recordings/door_camera/seg-1.mp4",
        "fregata/recordings/door_camera/seg-2.mp4",
    ]


def test_marks_the_event_complete(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")])
    w.run(w.events()[0])

    row = w.state.execute("SELECT * FROM event_delivery WHERE event_id='e1'").fetchone()
    assert row["completed_at"] is not None
    assert row["last_error"] is None
    assert row["manifest_key"] == "fregata/events/door_camera/e1/manifest.json"
    assert row["camera"] == "door_camera"


def test_records_whether_frigate_detected_a_face(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(
        events=[
            make_event(
                id="face-visible",
                data='{"attributes": [{"label": "face", "score": 0.91}]}',
            ),
            make_event(
                id="back-facing",
                start_time=NOW + 30.0,
                end_time=NOW + 50.0,
                data='{"attributes": []}',
            ),
        ],
        recordings=[seg_row("door_camera/seg-1.mp4")],
    )
    for event in w.events():
        w.run(event)

    rows = {
        row["event_id"]: row["face_detected"]
        for row in w.state.execute(
            "SELECT event_id,face_detected FROM event_delivery")
    }
    assert rows == {"face-visible": 1, "back-facing": 0}

def test_records_every_uploaded_segment(pipeline, segment_file):
    a = segment_file("door_camera/seg-1.mp4")
    b = segment_file("door_camera/seg-2.mp4")
    w = pipeline(events=[make_event(id="e1")],
                 recordings=[seg_row("door_camera/seg-1.mp4"), seg_row("door_camera/seg-2.mp4")])
    w.run(w.events()[0])

    rows = w.state.execute(
        "SELECT source_path,s3_key,etag,uploaded_at FROM segment_delivery ORDER BY s3_key").fetchall()
    assert [r["source_path"] for r in rows] == [str(a.resolve()), str(b.resolve())]
    assert all(r["etag"] and r["uploaded_at"] for r in rows)


def test_manifest_content_is_complete(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1", sub_label="Jericho", start_time=NOW, end_time=NOW + 20)],
                 recordings=[seg_row("door_camera/seg-1.mp4")])
    w.run(w.events()[0])

    body = w.client.get_object(Bucket="test-bucket",
                               Key="fregata/events/door_camera/e1/manifest.json")["Body"].read()
    m = json.loads(body)

    assert m["schema_version"] == 1
    assert m["source"] == "fregata-sqlite-reconciler"
    assert m["event"]["id"] == "e1"
    assert m["event"]["start_time_utc"] == rec.utc_iso(NOW)
    assert m["archive_window"]["pre_roll_seconds"] == 10.0
    assert m["archive_window"]["post_roll_seconds"] == 15.0
    assert m["archive_window"]["start_time"] == NOW - 10.0
    assert m["archive_window"]["end_time"] == NOW + 20.0 + 15.0
    assert len(m["segments"]) == 1
    assert m["segments"][0]["s3_key"] == "fregata/recordings/door_camera/seg-1.mp4"
    assert m["segments"][0]["etag"]


def test_padding_widens_the_segment_search(pipeline, segment_file):
    """A segment that only overlaps thanks to pre-roll must still be collected."""
    segment_file("door_camera/early.mp4")
    w = pipeline(
        events=[make_event(id="e1", start_time=NOW, end_time=NOW + 5)],
        recordings=[seg_row("door_camera/early.mp4", start_time=NOW - 20, end_time=NOW - 8)],
        pre_roll=30.0, post_roll=0.0,
    )
    w.run(w.events()[0])
    assert "fregata/recordings/door_camera/early.mp4" in w.keys()


def test_zero_padding_still_works(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")],
                 pre_roll=0.0, post_roll=0.0)
    w.run(w.events()[0])
    assert w.state.execute(
        "SELECT completed_at FROM event_delivery WHERE event_id='e1'").fetchone()["completed_at"]


def test_empty_prefix_produces_clean_keys(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")],
                 prefix="")
    w.run(w.events()[0])
    assert w.keys() == ["events/door_camera/e1/manifest.json", "recordings/door_camera/seg-1.mp4"]
    assert not any(k.startswith("/") for k in w.keys())


def test_manifest_can_be_disabled(pipeline, segment_file):
    """The privacy switch: no manifest means no sub_label leaves the house."""
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1", sub_label="Jericho")],
                 recordings=[seg_row("door_camera/seg-1.mp4")], upload_manifest=False)
    w.run(w.events()[0])

    assert w.keys() == ["fregata/recordings/door_camera/seg-1.mp4"]
    assert w.state.execute(
        "SELECT manifest_key FROM event_delivery WHERE event_id='e1'").fetchone()["manifest_key"] is None


# --------------------------------------------------------------------------
# idempotency / resumption
# --------------------------------------------------------------------------

def test_a_completed_event_is_skipped(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")])
    event = w.events()[0]
    w.run(event)
    first = w.state.execute("SELECT completed_at FROM event_delivery WHERE event_id='e1'").fetchone()[0]

    w.run(event)
    second = w.state.execute("SELECT completed_at FROM event_delivery WHERE event_id='e1'").fetchone()[0]
    assert first == second, "a completed event must not be re-delivered"


def test_a_partially_delivered_event_resumes_without_re_uploading(pipeline, segment_file):
    """Crash after segment 1, before completion: segment 1 must not be re-sent."""
    segment_file("door_camera/seg-1.mp4")
    segment_file("door_camera/seg-2.mp4")
    w = pipeline(events=[make_event(id="e1")],
                 recordings=[seg_row("door_camera/seg-1.mp4"), seg_row("door_camera/seg-2.mp4")])
    event = w.events()[0]

    real_upload = rec.upload_file
    calls: list[str] = []

    def fail_on_second(client, settings, source, key):
        calls.append(key)
        if len(calls) == 2:
            raise RuntimeError("uplink dropped")
        return real_upload(client, settings, source, key)

    rec.upload_file = fail_on_second
    try:
        with pytest.raises(RuntimeError, match="uplink dropped"):
            w.run(event)
    finally:
        rec.upload_file = real_upload

    assert w.state.execute("SELECT COUNT(*) FROM segment_delivery").fetchone()[0] == 1

    calls.clear()
    rec.upload_file = lambda *a, **k: calls.append(a[3]) or real_upload(*a, **k)
    try:
        w.run(event)
    finally:
        rec.upload_file = real_upload

    assert calls == ["fregata/recordings/door_camera/seg-2.mp4"], "segment 1 must be skipped"
    assert w.state.execute("SELECT completed_at FROM event_delivery WHERE event_id='e1'").fetchone()[0]


def test_resumption_reuses_the_recorded_etag(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")])
    event = w.events()[0]
    w.run(event)
    etag = w.state.execute("SELECT etag FROM segment_delivery").fetchone()["etag"]

    w.state.execute("UPDATE event_delivery SET completed_at=NULL WHERE event_id='e1'")
    w.state.commit()
    w.run(event)

    body = w.client.get_object(Bucket="test-bucket",
                               Key="fregata/events/door_camera/e1/manifest.json")["Body"].read()
    assert json.loads(body)["segments"][0]["etag"] == etag


def test_a_retried_event_clears_its_previous_error(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")])
    event = w.events()[0]
    w.state.execute(
        "INSERT INTO event_delivery(event_id,camera,start_time,end_time,last_error,updated_at)"
        " VALUES('e1','door_camera',1,2,'earlier failure',3)")
    w.state.commit()

    w.run(event)
    assert w.state.execute(
        "SELECT last_error FROM event_delivery WHERE event_id='e1'").fetchone()["last_error"] is None


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------

def test_dry_run_writes_nothing_to_s3_or_completion_state(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")],
                 dry_run=True)
    w.run(w.events()[0])

    assert w.keys() == []
    row = w.state.execute("SELECT * FROM event_delivery WHERE event_id='e1'").fetchone()
    assert row is not None, "the event is still recorded as seen"
    assert row["completed_at"] is None, "but never marked delivered"
    assert w.state.execute("SELECT COUNT(*) FROM segment_delivery").fetchone()[0] == 0


def test_dry_run_still_validates_segments(pipeline):
    """The rehearsal must catch a broken FREGATA_RECORDINGS_DIR."""
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/missing.mp4")],
                 dry_run=True)
    with pytest.raises(FileNotFoundError):
        w.run(w.events()[0])


def test_dry_run_then_live_delivers_everything(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")],
                 dry_run=True)
    w.run(w.events()[0])

    live = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")],
                    dry_run=False)
    live.run(live.events()[0])
    assert "fregata/recordings/door_camera/seg-1.mp4" in live.keys()


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------

def test_no_overlapping_segments_is_an_error(pipeline):
    w = pipeline(events=[make_event(id="e1", start_time=NOW, end_time=NOW + 20)],
                 recordings=[seg_row("door_camera/far.mp4",
                                     start_time=NOW + 9999, end_time=NOW + 99999)])
    with pytest.raises(RuntimeError, match="No recording segments overlap"):
        w.run(w.events()[0])

    row = w.state.execute("SELECT * FROM event_delivery WHERE event_id='e1'").fetchone()
    assert row is not None and row["completed_at"] is None


def test_missing_file_on_disk_is_an_error(pipeline):
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/ghost.mp4")])
    with pytest.raises(FileNotFoundError, match="Indexed recording does not exist"):
        w.run(w.events()[0])


def test_a_still_writing_segment_is_deferred(pipeline, segment_file):
    """Uploading a segment ffmpeg is still writing yields a truncated clip."""
    segment_file("door_camera/fresh.mp4", age=0.0)
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/fresh.mp4")],
                 settle_seconds=60.0)
    with pytest.raises(RuntimeError, match="still settling"):
        w.run(w.events()[0])
    assert w.keys() == [], "nothing may be uploaded from a deferred event"


def test_a_settled_segment_is_accepted(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4", age=120.0)
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")],
                 settle_seconds=60.0)
    w.run(w.events()[0])
    assert "fregata/recordings/door_camera/seg-1.mp4" in w.keys()


def test_one_unsettled_segment_blocks_the_whole_event(pipeline, segment_file):
    """Deliberate: a partial clip is worse than a late one. But note the cost —
    segment 1 is uploaded and paid for on every retry attempt until segment 2
    settles."""
    segment_file("door_camera/a.mp4", age=600.0)
    segment_file("door_camera/b.mp4", age=0.0)
    w = pipeline(events=[make_event(id="e1")],
                 recordings=[seg_row("door_camera/a.mp4"), seg_row("door_camera/b.mp4")],
                 settle_seconds=60.0)
    with pytest.raises(RuntimeError, match="still settling"):
        w.run(w.events()[0])

    assert w.keys() == ["fregata/recordings/door_camera/a.mp4"]
    assert w.state.execute(
        "SELECT completed_at FROM event_delivery WHERE event_id='e1'").fetchone()[0] is None


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "A BLOB in the event's `data` column survives parse_json as bytes, then "
    "json.dumps raises TypeError while building the manifest — after the "
    "segments have already been uploaded and billed. The event can never "
    "complete, so it retries forever."))
def test_a_blob_data_column_does_not_break_the_manifest(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1", data=b"\x89PNG binary thumbnail")],
                 recordings=[seg_row("door_camera/seg-1.mp4")])
    w.run(w.events()[0])
    assert "fregata/events/door_camera/e1/manifest.json" in w.keys()


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "A permanently broken event (deleted segment file) is retried on every "
    "poll forever. There is no attempt counter, no backoff and no dead-letter "
    "state, so one bad event re-logs a traceback every POLL_SECONDS "
    "indefinitely."))
def test_a_permanently_failing_event_is_eventually_given_up_on(pipeline):
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/ghost.mp4")])
    event = w.events()[0]
    for _ in range(50):
        with pytest.raises(FileNotFoundError):
            w.run(event)

    row = w.state.execute("SELECT * FROM event_delivery WHERE event_id='e1'").fetchone()
    assert "attempts" in row.keys() or "abandoned_at" in row.keys()


# --------------------------------------------------------------------------
# Notion hand-off from process_event
# --------------------------------------------------------------------------

def test_completed_event_pushes_the_real_segment_count_to_notion(pipeline, segment_file, monkeypatch):
    segment_file("door_camera/seg-1.mp4")
    segment_file("door_camera/seg-2.mp4")
    w = pipeline(events=[make_event(id="e1")],
                 recordings=[seg_row("door_camera/seg-1.mp4"), seg_row("door_camera/seg-2.mp4")],
                 notion_token="t", notion_database_id="d")

    seen = {}
    monkeypatch.setattr(rec, "sync_notion",
                        lambda ev, key, segs, st, se: seen.update(key=key, segs=segs))
    w.run(w.events()[0])
    assert seen == {"key": "fregata/events/door_camera/e1/manifest.json", "segs": 2}


def test_an_already_complete_event_still_gets_a_notion_retry(pipeline, segment_file, monkeypatch):
    """Notion may have been added, or been down, after S3 delivery succeeded."""
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")])
    event = w.events()[0]
    w.run(event)

    seen = {}
    monkeypatch.setattr(rec, "sync_notion",
                        lambda ev, key, segs, st, se: seen.update(key=key, segs=segs))
    w.run(event)
    assert seen == {"key": "fregata/events/door_camera/e1/manifest.json", "segs": 1}


def test_notion_failure_does_not_undo_a_completed_delivery(pipeline, segment_file, monkeypatch):
    """S3 is the product; Notion is a notification. Losing the index must not
    make the archive look undelivered."""
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")],
                 notion_token="t", notion_database_id="d")

    def boom(*a, **k):
        raise RuntimeError("Notion 503")

    monkeypatch.setattr(rec, "sync_notion", boom)
    with pytest.raises(RuntimeError, match="Notion 503"):
        w.run(w.events()[0])

    row = w.state.execute("SELECT * FROM event_delivery WHERE event_id='e1'").fetchone()
    assert row["completed_at"] is not None, "S3 delivery already succeeded"
    assert "fregata/recordings/door_camera/seg-1.mp4" in w.keys()


# --------------------------------------------------------------------------
# path handling in anger
# --------------------------------------------------------------------------

def test_segments_outside_the_media_root_land_under_external(pipeline, tmp_path):
    """Documents the mis-configured-FREGATA_RECORDINGS_DIR blast radius."""
    stray = tmp_path / "other-disk" / "seg.mp4"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"data")
    old = os.stat(stray).st_mtime - 600
    os.utime(stray, (old, old))

    w = pipeline(events=[make_event(id="e1")],
                 recordings=[make_recording(id="r", camera="door_camera", path=str(stray),
                                            start_time=NOW - 30, end_time=NOW + 30)])
    w.run(w.events()[0])
    assert any(k.startswith("fregata/recordings/external/") for k in w.keys())


def test_the_same_segment_shared_by_two_events_is_tracked_per_event(pipeline, segment_file):
    segment_file("door_camera/shared.mp4")
    w = pipeline(
        events=[make_event(id="e1", start_time=NOW, end_time=NOW + 5),
                make_event(id="e2", start_time=NOW + 6, end_time=NOW + 10)],
        recordings=[seg_row("door_camera/shared.mp4", start_time=NOW - 30, end_time=NOW + 30)],
    )
    for event in w.events():
        w.run(event)

    rows = w.state.execute("SELECT event_id FROM segment_delivery ORDER BY event_id").fetchall()
    assert [r["event_id"] for r in rows] == ["e1", "e2"]
    assert w.keys().count("fregata/recordings/door_camera/shared.mp4") == 1, "one object, not two"


# --------------------------------------------------------------------------
# Slack summary bookkeeping
# --------------------------------------------------------------------------

def test_records_person_and_recorded_at_for_the_slack_summary(pipeline, segment_file):
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(
        events=[make_event(id="known", sub_label="Alice"),
                make_event(id="stranger", sub_label=None)],
        recordings=[seg_row("door_camera/seg-1.mp4")],
    )
    for e in w.events():
        w.run(e)

    rows = {r["event_id"]: r for r in w.state.execute(
        "SELECT event_id,person,recorded_at FROM event_delivery")}
    assert rows["known"]["person"] == "Alice"
    assert rows["stranger"]["person"] is None, "a stranger is the summary's whole subject"
    assert all(r["recorded_at"] is not None for r in rows.values())


def test_recorded_at_survives_reprocessing(pipeline, segment_file):
    """recorded_at scopes an event into exactly one summary window; a retried
    delivery must not shift it forward into a later window."""
    segment_file("door_camera/seg-1.mp4")
    w = pipeline(events=[make_event(id="e1")], recordings=[seg_row("door_camera/seg-1.mp4")])
    event = w.events()[0]
    w.run(event)
    first = w.state.execute("SELECT recorded_at FROM event_delivery WHERE event_id='e1'").fetchone()[0]
    w.state.execute("UPDATE event_delivery SET completed_at=NULL WHERE event_id='e1'")
    w.state.commit()
    w.run(event)
    second = w.state.execute("SELECT recorded_at FROM event_delivery WHERE event_id='e1'").fetchone()[0]
    assert second == first
