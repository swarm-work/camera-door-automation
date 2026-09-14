"""The commands, their exit codes, and their resource hygiene."""
from __future__ import annotations

import json
import sqlite3
import tempfile

import pytest

import reconciler as rec
from conftest import NOW, build_source_db, make_event, make_recording


@pytest.fixture
def use_session_s3(monkeypatch, s3):
    """run_once builds its own S3 client; point it at the moto-backed one."""
    monkeypatch.setattr(rec, "s3_client", lambda settings: s3)
    return s3


@pytest.fixture
def source_with(settings_factory):
    def _build(events=(), recordings=(), **over):
        settings = settings_factory(**over)
        build_source_db(settings.source_db, events=events, recordings=recordings)
        return settings
    return _build


def seg_row(rel: str, **over):
    row = dict(id=rel, camera="door_camera", path=f"/media/frigate/recordings/{rel}",
               start_time=NOW - 30.0, end_time=NOW + 30.0)
    row.update(over)
    return make_recording(**row)


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def test_inspect_reports_the_discovered_schema(source_with, capsys):
    settings = source_with(events=[make_event()], recordings=[make_recording()])
    assert rec.inspect(settings) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["event_table"] == "event"
    assert out["recording_table"] == "recordings"
    assert "start_time" in out["event_columns"]
    assert "path" in out["recording_columns"]
    assert out["source_db"] == str(settings.source_db)


def test_inspect_is_read_only(source_with):
    settings = source_with(events=[make_event()])
    before = settings.source_db.read_bytes()
    rec.inspect(settings)
    assert settings.source_db.read_bytes() == before


def test_inspect_cleans_up_its_snapshot(source_with, monkeypatch, tmp_path, capsys):
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    rec.inspect(source_with(events=[make_event()]))
    assert list(spool.iterdir()) == []


def test_inspect_fails_clearly_on_a_missing_database(settings_factory):
    with pytest.raises(FileNotFoundError):
        rec.inspect(settings_factory())


def test_inspect_creates_no_state_database(source_with):
    settings = source_with(events=[make_event()])
    rec.inspect(settings)
    assert not settings.state_db.exists(), "inspect must be a pure read"


# --------------------------------------------------------------------------
# run_once
# --------------------------------------------------------------------------

def test_run_once_delivers_and_returns_zero(source_with, use_session_s3, segment_file):
    segment_file("door_camera/seg-1.mp4")
    settings = source_with(events=[make_event(id="e1")],
                           recordings=[seg_row("door_camera/seg-1.mp4")])
    assert rec.run_once(settings) == 0

    keys = {o["Key"] for o in use_session_s3.list_objects_v2(Bucket="test-bucket")["Contents"]}
    assert keys == {"fregata/recordings/door_camera/seg-1.mp4",
                    "fregata/events/door_camera/e1/manifest.json"}


def test_run_once_returns_zero_when_there_is_nothing_to_do(source_with, use_session_s3):
    assert rec.run_once(source_with()) == 0


def test_run_once_returns_one_when_an_event_fails(source_with, use_session_s3):
    settings = source_with(events=[make_event(id="e1")],
                           recordings=[seg_row("door_camera/ghost.mp4")])
    assert rec.run_once(settings) == 1


def test_one_bad_event_does_not_block_the_others(source_with, use_session_s3, segment_file):
    """The core resilience property of the pass."""
    segment_file("door_camera/good.mp4")
    settings = source_with(
        events=[make_event(id="bad", start_time=NOW, end_time=NOW + 5),
                make_event(id="good", start_time=NOW + 100, end_time=NOW + 105)],
        recordings=[seg_row("door_camera/ghost.mp4", start_time=NOW - 30, end_time=NOW + 30),
                    seg_row("door_camera/good.mp4", start_time=NOW + 90, end_time=NOW + 150)],
    )
    assert rec.run_once(settings) == 1

    state = rec.open_state(settings.state_db)
    try:
        rows = {r["event_id"]: r for r in state.execute("SELECT * FROM event_delivery")}
        assert rows["good"]["completed_at"] is not None
        assert rows["bad"]["completed_at"] is None
        assert "does not exist" in rows["bad"]["last_error"]
    finally:
        state.close()


def test_run_once_records_the_failure_reason(source_with, use_session_s3):
    settings = source_with(events=[make_event(id="e1")],
                           recordings=[seg_row("door_camera/ghost.mp4")])
    rec.run_once(settings)

    state = rec.open_state(settings.state_db)
    try:
        err = state.execute("SELECT last_error FROM event_delivery WHERE event_id='e1'").fetchone()[0]
        assert "Indexed recording does not exist" in err
    finally:
        state.close()


def test_run_once_cleans_up_its_snapshot(source_with, use_session_s3, monkeypatch, tmp_path, segment_file):
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    segment_file("door_camera/seg-1.mp4")
    rec.run_once(source_with(events=[make_event(id="e1")],
                             recordings=[seg_row("door_camera/seg-1.mp4")]))
    assert list(spool.iterdir()) == [], "a snapshot leaked; watch would fill /tmp"


def test_run_once_cleans_up_even_when_an_event_fails(source_with, use_session_s3, monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))
    rec.run_once(source_with(events=[make_event(id="e1")],
                             recordings=[seg_row("door_camera/ghost.mp4")]))
    assert list(spool.iterdir()) == []


def test_run_once_ends_with_the_clip_refresh_pass(source_with, use_session_s3, monkeypatch):
    """Deliveries first, links second: a slow Notion day must not delay footage.
    The ORDER is the property — a refresh moved ahead of the event loop would pass
    a call-count check while putting Notion on the delivery critical path."""
    order = []
    monkeypatch.setattr(rec, "process_event", lambda *a, **k: order.append("deliver"))
    monkeypatch.setattr(rec, "refresh_clip_links", lambda *a, **k: order.append("refresh"))
    settings = source_with(events=[make_event(id="e1")],
                           recordings=[seg_row("door_camera/seg-1.mp4")])
    rec.run_once(settings)
    assert order == ["deliver", "refresh"]


def test_run_once_is_idempotent_across_passes(source_with, use_session_s3, segment_file):
    """`watch` calls this every POLL_SECONDS; pass two must be a no-op."""
    segment_file("door_camera/seg-1.mp4")
    settings = source_with(events=[make_event(id="e1")],
                           recordings=[seg_row("door_camera/seg-1.mp4")])
    rec.run_once(settings)

    state = rec.open_state(settings.state_db)
    first = state.execute("SELECT completed_at FROM event_delivery WHERE event_id='e1'").fetchone()[0]
    state.close()

    assert rec.run_once(settings) == 0
    state = rec.open_state(settings.state_db)
    try:
        assert state.execute(
            "SELECT completed_at FROM event_delivery WHERE event_id='e1'").fetchone()[0] == first
    finally:
        state.close()


def test_run_once_propagates_a_missing_source_database(settings_factory, use_session_s3):
    """`watch` relies on this bubbling up so the pass is logged and retried."""
    with pytest.raises(FileNotFoundError):
        rec.run_once(settings_factory())


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "run_once's error handler is an UPDATE. If the event fails BEFORE "
    "process_event inserts its row — e.g. a corrupt start_time — the UPDATE "
    "matches zero rows and the failure is silently dropped from the state DB. "
    "`status` then reports a clean system while the event is never delivered."))
def test_a_failure_before_the_first_insert_is_still_recorded(source_with, use_session_s3):
    settings = source_with(events=[make_event(id="e1", start_time="corrupt")],
                           recordings=[seg_row("door_camera/seg-1.mp4")])
    assert rec.run_once(settings) == 1

    state = rec.open_state(settings.state_db)
    try:
        row = state.execute("SELECT last_error FROM event_delivery WHERE event_id='e1'").fetchone()
        assert row is not None and row["last_error"], "the failure vanished from state"
    finally:
        state.close()


def test_every_event_is_rescanned_on_every_pass(source_with, use_session_s3, segment_file):
    """Documents an unbounded cost: fetch_events has no 'since' bound, so a
    watch loop re-reads and re-checks the entire event history every 30s,
    forever. Fine at 50 events, not at 500k."""
    segment_file("door_camera/seg-1.mp4")
    events = [make_event(id=f"e{i}", start_time=NOW + i, end_time=NOW + i + 1) for i in range(40)]
    settings = source_with(events=events, recordings=[seg_row("door_camera/seg-1.mp4")])
    rec.run_once(settings)

    snap = rec.snapshot_db(settings.source_db)
    try:
        conn = sqlite3.connect(snap)
        conn.row_factory = sqlite3.Row
        table, cols = rec.resolve_table(conn, ("event",), rec.EVENT_REQUIRED)
        assert len(rec.fetch_events(conn, table, cols, settings)) == 40, (
            "completed events are still returned by the query on every pass")
        conn.close()
    finally:
        snap.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def test_status_on_a_fresh_install(settings_factory, capsys):
    assert rec.status(settings_factory()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"events_seen": 0, "events_complete": 0, "events_failed": 0,
                   "segments_uploaded": 0, "event_clips_uploaded": 0,
                   "event_clips_failed": 0, "notion_synced": 0,
                   "notion_failed": 0, "notion_gave_up": 0, "clip_fresh": 0,
                   "clip_stale": 0, "clip_gave_up": 0,
                   "slack_last_summary": None, "dry_run": False}


def test_status_counts_each_category(source_with, use_session_s3, segment_file, capsys):
    segment_file("door_camera/good.mp4")
    settings = source_with(
        events=[make_event(id="good", start_time=NOW, end_time=NOW + 5),
                make_event(id="bad", start_time=NOW + 100, end_time=NOW + 105)],
        recordings=[seg_row("door_camera/good.mp4", start_time=NOW - 30, end_time=NOW + 30)],
    )
    rec.run_once(settings)
    capsys.readouterr()

    assert rec.status(settings) == 0
    out = capsys.readouterr().out
    counts = json.loads(out[:out.index("\nFAILED")])
    assert counts["events_seen"] == 2
    assert counts["events_complete"] == 1
    assert counts["events_failed"] == 1
    assert counts["segments_uploaded"] == 1
    assert "FAILED bad:" in out


def test_status_reports_dry_run_mode(settings_factory, capsys):
    rec.status(settings_factory(dry_run=True))
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_status_lists_at_most_ten_failures(settings_factory, capsys):
    settings = settings_factory()
    state = rec.open_state(settings.state_db)
    for i in range(25):
        state.execute(
            "INSERT INTO event_delivery(event_id,camera,start_time,end_time,last_error,updated_at)"
            " VALUES(?,?,?,?,?,?)", (f"e{i}", "c", 1, 2, "boom", i))
    state.commit()
    state.close()

    rec.status(settings)
    out = capsys.readouterr().out
    assert json.loads(out[:out.index("\nFAILED")])["events_failed"] == 25
    assert out.count("FAILED ") == 10, "the summary must stay readable"


def test_status_shows_the_most_recent_failures_first(settings_factory, capsys):
    settings = settings_factory()
    state = rec.open_state(settings.state_db)
    for i in range(12):
        state.execute(
            "INSERT INTO event_delivery(event_id,camera,start_time,end_time,last_error,updated_at)"
            " VALUES(?,?,?,?,?,?)", (f"e{i}", "c", 1, 2, "boom", i))
    state.commit()
    state.close()

    rec.status(settings)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("FAILED")]
    assert lines[0].startswith("FAILED e11")


def test_status_creates_the_state_db_if_absent(settings_factory, capsys):
    settings = settings_factory()
    assert not settings.state_db.exists()
    rec.status(settings)
    assert settings.state_db.exists()


# --------------------------------------------------------------------------
# clips-reset
# --------------------------------------------------------------------------

def test_clips_reset_clears_clip_state_on_synced_rows_only(settings_factory, capsys):
    settings = settings_factory()
    state = rec.open_state(settings.state_db)
    state.execute("INSERT INTO notion_delivery(event_id,page_id,synced_at,clip_signed_at,clip_attempts,updated_at)"
                  " VALUES('synced','p',1,2,5,0)")
    state.execute("INSERT INTO notion_delivery(event_id,attempts,clip_attempts,updated_at)"
                  " VALUES('unsynced',5,3,0)")
    state.commit()
    state.close()

    assert rec.clips_reset(settings) == 0
    assert "1 synced page" in capsys.readouterr().out

    state = rec.open_state(settings.state_db)
    try:
        synced = state.execute("SELECT * FROM notion_delivery WHERE event_id='synced'").fetchone()
        unsynced = state.execute("SELECT * FROM notion_delivery WHERE event_id='unsynced'").fetchone()
    finally:
        state.close()
    assert synced["clip_signed_at"] is None and synced["clip_attempts"] == 0
    assert unsynced["clip_attempts"] == 3, "no page yet — nothing to re-sign"
    assert unsynced["attempts"] == 5, "the creation budget is not clip state"


# --------------------------------------------------------------------------
# main / argument handling
# --------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, settings_factory):
    def _run(argv, **over):
        settings = settings_factory(**over)
        monkeypatch.setattr("sys.argv", ["reconciler.py", *argv])
        monkeypatch.setattr(rec.Settings, "from_env", staticmethod(lambda: settings))
        return rec.main()
    return _run


@pytest.mark.parametrize("command,target", [
    ("inspect", "inspect"), ("once", "run_once"), ("status", "status"),
    ("clips-reset", "clips_reset"),
])
def test_each_command_dispatches(monkeypatch, cli, command, target):
    called = []
    monkeypatch.setattr(rec, target, lambda s: called.append(target) or 7)
    assert cli([command]) == 7
    assert called == [target]


def test_slack_people_summary_command_dispatches_with_date(monkeypatch, cli):
    called = []
    monkeypatch.setattr(rec, "slack_people_summary_now",
                        lambda s, target_date=None: called.append(target_date) or 7)
    assert cli(["slack-people-summary", "2026-08-19"]) == 7
    assert called == ["2026-08-19"]

def test_event_clip_backfill_dispatches_with_date_and_apply(monkeypatch, cli):
    called = []
    monkeypatch.setattr(
        rec, "event_clips_backfill",
        lambda settings, target_date, apply=False:
        called.append((target_date, apply)) or 7)
    assert cli(["event-clips-backfill", "--date", "2026-08-19", "--apply"]) == 7
    assert called == [("2026-08-19", True)]

def test_an_unknown_command_is_rejected(cli):
    with pytest.raises(SystemExit) as exc:
        cli(["frobnicate"])
    assert exc.value.code == 2


def test_a_missing_command_is_rejected(cli):
    with pytest.raises(SystemExit) as exc:
        cli([])
    assert exc.value.code == 2


def test_watch_loops_until_interrupted(monkeypatch, cli):
    passes = []

    def one_pass(settings):
        passes.append(1)
        if len(passes) == 3:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(rec, "run_once", one_pass)
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    assert cli(["watch"]) == 130
    assert len(passes) == 3


def test_watch_survives_a_failing_pass(monkeypatch, cli, caplog):
    """A transient NVR outage must not kill the service."""
    passes = []

    def flaky(settings):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("database is locked")
        raise KeyboardInterrupt

    monkeypatch.setattr(rec, "run_once", flaky)
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    with caplog.at_level("ERROR"):
        assert cli(["watch"]) == 130
    assert len(passes) == 2
    assert "Reconciliation pass failed" in caplog.text


def test_watch_sleeps_for_the_configured_interval(monkeypatch, cli):
    slept = []
    passes = []

    def one_pass(settings):
        passes.append(1)
        if len(passes) == 2:
            raise KeyboardInterrupt
        return 0

    monkeypatch.setattr(rec, "run_once", one_pass)
    monkeypatch.setattr(rec.time, "sleep", lambda s: slept.append(s))
    cli(["watch"], poll_seconds=17.0)
    assert slept == [17.0]


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "In main(), time.sleep sits OUTSIDE the try block, so Ctrl-C during the "
    "poll interval — which is where the process spends nearly all its time — "
    "escapes as an unhandled KeyboardInterrupt traceback instead of the clean "
    "130 exit the in-pass handler was written to give."))
def test_ctrl_c_during_the_sleep_exits_cleanly(monkeypatch, cli):
    def interrupt_the_nap(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(rec, "run_once", lambda s: 0)
    monkeypatch.setattr(rec.time, "sleep", interrupt_the_nap)
    try:
        code = cli(["watch"])
    except KeyboardInterrupt:
        # Caught here rather than left to propagate: KeyboardInterrupt is a
        # BaseException, so letting it escape would abort the whole pytest run
        # instead of registering as this test's expected failure.
        pytest.fail("Ctrl-C during the poll interval escaped as an unhandled "
                    "KeyboardInterrupt instead of exiting 130")
    assert code == 130


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "`watch` discards run_once's return value, so a launchd-managed service "
    "has no signal that events are failing. Only `status` reveals it, and "
    "nothing runs `status`."))
def test_watch_surfaces_persistent_failures(monkeypatch, cli, caplog):
    passes = []

    def always_failing(settings):
        passes.append(1)
        if len(passes) == 5:
            raise KeyboardInterrupt
        return 1                       # every pass reports failed events

    monkeypatch.setattr(rec, "run_once", always_failing)
    monkeypatch.setattr(rec.time, "sleep", lambda s: None)
    with caplog.at_level("WARNING"):
        cli(["watch"])
    assert any("fail" in r.message.lower() for r in caplog.records)
