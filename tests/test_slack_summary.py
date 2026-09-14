"""The Slack end-of-day summary: window arithmetic, once-per-day gating, and
what may never appear by default (names, presigned URLs).

The cursor in ``meta`` is both the dedupe marker and the left edge of the next
window, so most tests pivot on where events' ``recorded_at`` falls relative to it.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
import responses

import reconciler as rec
from conftest import NOW, build_source_db, make_event

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


@pytest.fixture
def slack_settings(settings_factory):
    return settings_factory(dry_run=False, slack_webhook_url=WEBHOOK)


@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as m:
        yield m


def seed_event(state, event_id, *, person=None, face_detected=False,
               recorded_at=NOW, camera="door_camera",
               start=NOW, end=NOW + 20.0, completed=True):
    state.execute("""INSERT INTO event_delivery(event_id,camera,start_time,end_time,person,face_detected,recorded_at,completed_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (event_id, camera, start, end, person, int(face_detected), recorded_at,
                   NOW if completed else None, NOW))
    state.commit()


def posted_text(http) -> str:
    assert len(http.calls) == 1, "expected exactly one Slack post"
    body = json.loads(http.calls[0].request.body)
    return json.dumps(body)


def anchor(settings):
    """A ``now`` that is past today's scheduled time, plus a cursor before it."""
    scheduled = rec.summary_scheduled_epoch(NOW, settings)
    return scheduled + 60.0, scheduled - 3600.0


# --------------------------------------------------------------------------
# Schema and bookkeeping
# --------------------------------------------------------------------------

def test_meta_roundtrip(state_db):
    assert rec.get_meta(state_db, "k") is None
    rec.set_meta(state_db, "k", "v1")
    rec.set_meta(state_db, "k", "v2")
    assert rec.get_meta(state_db, "k") == "v2"


def test_old_state_db_gains_the_new_columns(settings_factory):
    """A state DB written before this feature must migrate in place."""
    path = settings_factory().state_db
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE event_delivery (
        event_id TEXT PRIMARY KEY, camera TEXT NOT NULL, start_time REAL NOT NULL,
        end_time REAL NOT NULL, manifest_key TEXT, completed_at REAL,
        last_error TEXT, updated_at REAL NOT NULL)""")
    conn.commit()
    conn.close()
    state = rec.open_state(path)
    try:
        cols = rec.table_columns(state, "event_delivery")
        assert {"person", "face_detected", "recorded_at"} <= cols
        rec.set_meta(state, "probe", "ok")
    finally:
        state.close()


def test_recognized_person_extraction():
    assert rec.recognized_person({"sub_label": "Alice"}) == "Alice"
    assert rec.recognized_person({"sub_label": "  "}) is None
    assert rec.recognized_person({"sub_label": None}) is None
    assert rec.recognized_person({}) is None
    assert rec.recognized_person({"sub_label": ["Alice", 0.9]}) is None

def test_detected_face_extraction():
    assert rec.has_detected_face({
        "data": {"attributes": [{"label": "face", "score": 0.91}]}})
    assert rec.has_detected_face({
        "data": '{"attributes": [{"label": "FACE", "score": 0.91}]}'})
    assert not rec.has_detected_face({"data": {"attributes": []}})
    assert not rec.has_detected_face({"data": {"attributes": None}})
    assert not rec.has_detected_face({"data": {"attributes": [{"label": "hat"}]}})
    assert not rec.has_detected_face({})


# --------------------------------------------------------------------------
# Gating: when a summary is due
# --------------------------------------------------------------------------

def test_first_pass_opens_the_window_without_posting(state_db, slack_settings, http):
    """Enabling Slack must not dump the whole historical backlog into one message."""
    seed_event(state_db, "old-1", recorded_at=NOW - 999.0)
    now, _ = anchor(slack_settings)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)
    assert len(http.calls) == 0
    assert rec.get_meta(state_db, rec.SLACK_SENT_AT_KEY) == str(now)
    # ...and the pre-enable event stays out of the next summary too.
    later = now + 60.0
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=later)
    assert len(http.calls) == 0, "nothing new was recorded, so nothing is due to post"


def test_not_due_before_the_scheduled_time(state_db, slack_settings, http):
    scheduled = rec.summary_scheduled_epoch(NOW, slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(scheduled - 7200.0))
    seed_event(state_db, "e1", recorded_at=scheduled - 3600.0)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=scheduled - 60.0)
    assert len(http.calls) == 0


def test_due_summary_posts_once(state_db, slack_settings, http):
    now, cursor = anchor(slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", recorded_at=cursor + 60.0)
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)
    text = posted_text(http)
    assert "1 unknown visitor" in text
    assert "door_camera" in text
    assert rec.get_meta(state_db, rec.SLACK_SENT_AT_KEY) == str(now)
    # A second pass the same evening is a no-op.
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now + 30.0)
    assert len(http.calls) == 1


def test_recognized_people_are_excluded(state_db, slack_settings, http):
    now, cursor = anchor(slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "known", person="Alice", recorded_at=cursor + 10.0)
    seed_event(state_db, "stranger", recorded_at=cursor + 20.0)
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)
    text = posted_text(http)
    assert "1 unknown visitor" in text
    assert "Alice" not in text, "a recognized name must never reach Slack"

def test_face_filter_excludes_back_facing_unknown_person(state_db, settings_factory, http):
    settings = settings_factory(
        dry_run=False,
        slack_webhook_url=WEBHOOK,
        slack_unknown_requires_face=True,
    )
    now, cursor = anchor(settings)
    build_source_db(settings.source_db, events=[
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
    ])
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "face-visible", recorded_at=cursor + 10.0)
    seed_event(state_db, "back-facing", recorded_at=cursor + 20.0)
    state_db.execute("""INSERT INTO notion_delivery(event_id,page_id,synced_at,updated_at)
                        VALUES('face-visible','visible-page-id',?,?)""", (NOW, NOW))
    state_db.execute("""INSERT INTO notion_delivery(event_id,page_id,synced_at,updated_at)
                        VALUES('back-facing','back-page-id',?,?)""", (NOW, NOW))
    state_db.commit()

    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, settings, now=now)

    text = posted_text(http)
    assert "1 unknown visitor" in text
    assert "visiblepageid" in text
    assert "backpageid" not in text
    rows = {
        row["event_id"]: row["face_detected"]
        for row in state_db.execute(
            "SELECT event_id,face_detected FROM event_delivery")
    }
    assert rows == {"face-visible": 1, "back-facing": 0}


def test_quiet_day_posts_nothing_but_advances_the_cursor(state_db, slack_settings, http):
    now, cursor = anchor(slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "known", person="Alice", recorded_at=cursor + 10.0)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)
    assert len(http.calls) == 0
    assert rec.get_meta(state_db, rec.SLACK_SENT_AT_KEY) == str(now)


def test_on_empty_true_posts_a_zero_line(state_db, settings_factory, http):
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK,
                                slack_summary_on_empty=True)
    now, cursor = anchor(settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, settings, now=now)
    assert "no unknown visitors" in posted_text(http)


def test_failed_post_keeps_the_cursor_for_a_retry(state_db, slack_settings, http):
    now, cursor = anchor(slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", recorded_at=cursor + 60.0)
    http.add(responses.POST, WEBHOOK, body="no_service", status=500)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)
    assert rec.get_meta(state_db, rec.SLACK_SENT_AT_KEY) == str(cursor)
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now + 30.0)
    assert len(http.calls) == 2
    assert rec.get_meta(state_db, rec.SLACK_SENT_AT_KEY) == str(now + 30.0)


def test_dry_run_never_posts(state_db, settings_factory, http):
    settings = settings_factory(dry_run=True, slack_webhook_url=WEBHOOK)
    now, cursor = anchor(settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", recorded_at=cursor + 60.0)
    rec.maybe_send_slack_summary(state_db, settings, now=now)
    assert len(http.calls) == 0


def test_summary_failure_never_raises(state_db, slack_settings, http):
    """A corrupt cursor value must not take down the delivery loop."""
    now, _ = anchor(slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, "not-a-number")
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)  # must not raise
    assert len(http.calls) == 0


# --------------------------------------------------------------------------
# Message content
# --------------------------------------------------------------------------

def test_message_lines_and_notion_link(state_db, slack_settings, http):
    now, cursor = anchor(slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", recorded_at=cursor + 10.0, start=NOW, end=NOW + 42.0)
    seed_event(state_db, "e2", recorded_at=cursor + 20.0, completed=False)
    state_db.execute("""INSERT INTO notion_delivery(event_id,page_id,synced_at,updated_at)
                        VALUES('e1','abc-123-def',?,?)""", (NOW, NOW))
    state_db.commit()
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)
    text = posted_text(http)
    assert "2 unknown visitor" in text
    assert "42s" in text
    assert "<https://www.notion.so/abc123def|Notion>" in text
    assert "footage not yet uploaded" in text
    assert "AWSAccessKeyId" not in text and "X-Amz-" not in text



def test_snapshot_url_renders_as_event_accessory(state_db, slack_settings):
    seed_event(state_db, "e1", recorded_at=NOW - 10.0, start=NOW, end=NOW + 42.0)
    state_db.execute("""INSERT INTO notion_delivery(event_id,page_id,synced_at,updated_at)
                        VALUES('e1','abc-123-def',?,?)""", (NOW, NOW))
    state_db.commit()
    rows = rec.unknown_events_between(state_db, NOW - 20.0, NOW)

    payload = rec.render_slack_summary(
        rows, NOW, snapshot_urls={"e1": "https://example.test/snapshot.jpg"})

    body = json.dumps(payload)
    event_block = next(b for b in payload["blocks"] if b.get("accessory"))
    assert event_block["accessory"]["image_url"] == "https://example.test/snapshot.jpg"
    assert "<https://www.notion.so/abc123def|Notion>" in body
    assert "42s" in body


def test_snapshot_uploads_and_presigns_when_enabled(settings_factory, s3):
    settings = settings_factory(dry_run=False, slack_include_snapshots=True)
    clips_dir = settings.recordings_dir.parent / "clips"
    clips_dir.mkdir()
    (clips_dir / "door_camera-e1.jpg").write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
    state = rec.open_state(settings.state_db)
    try:
        seed_event(state, "e1", recorded_at=NOW)
        rows = rec.unknown_events_between(state, NOW - 1.0, NOW + 1.0)
        urls = rec.slack_snapshot_urls(rows, settings, client=s3, signer=s3)
    finally:
        state.close()

    assert list(urls) == ["e1"]
    assert "X-Amz-Signature" in urls["e1"]
    objects = s3.list_objects_v2(Bucket=settings.bucket, Prefix="fregata/slack-snapshots/door_camera/e1.jpg")
    assert objects["KeyCount"] == 1

def test_camera_names_are_slack_escaped(state_db, slack_settings, http):
    now, cursor = anchor(slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", camera="door<&>cam", recorded_at=cursor + 10.0)
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)
    assert "door&lt;&amp;&gt;cam" in posted_text(http)


def test_long_days_are_truncated_with_a_count(state_db, slack_settings, http):
    now, cursor = anchor(slack_settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    for i in range(rec.SLACK_SUMMARY_MAX_LINES + 5):
        seed_event(state_db, f"e{i}", recorded_at=cursor + 10.0 + i)
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, slack_settings, now=now)
    text = posted_text(http)
    assert f"{rec.SLACK_SUMMARY_MAX_LINES + 5} unknown visitor" in text
    assert "and 5 more" in text


# --------------------------------------------------------------------------
# The slack-summary command
# --------------------------------------------------------------------------

def test_slack_summary_now_posts_and_marks(settings_factory, http):
    import time as _time
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK)
    state = rec.open_state(settings.state_db)
    try:
        # slack_summary_now runs on the real clock, and with no cursor yet it covers
        # the last 24h — so the seeded event must sit inside that real window.
        seed_event(state, "e1", recorded_at=_time.time() - 10.0)
    finally:
        state.close()
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    assert rec.slack_summary_now(settings) == 0
    assert "1 unknown visitor" in posted_text(http)
    state = rec.open_state(settings.state_db)
    try:
        assert rec.get_meta(state, rec.SLACK_SENT_AT_KEY) is not None
    finally:
        state.close()
    assert len(http.calls) == 1


def test_slack_summary_now_dry_run_prints_instead(settings_factory, http, capsys):
    settings = settings_factory(dry_run=True, slack_webhook_url=WEBHOOK)
    state = rec.open_state(settings.state_db)
    try:
        seed_event(state, "e1", recorded_at=NOW - 10.0)
    finally:
        state.close()
    assert rec.slack_summary_now(settings) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert len(http.calls) == 0


def test_slack_summary_now_without_webhook_fails(settings_factory, capsys):
    settings = settings_factory(dry_run=False, slack_webhook_url=None)
    assert rec.slack_summary_now(settings) == 1
    assert "SLACK_WEBHOOK_URL" in capsys.readouterr().out



# --------------------------------------------------------------------------
# Known visitors section (SLACK_INCLUDE_KNOWN)
# --------------------------------------------------------------------------

def test_known_visitors_are_included_when_enabled(state_db, settings_factory, http):
    """With SLACK_INCLUDE_KNOWN=true, recognized people appear in a dedicated section."""
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK, slack_include_known=True)
    now, cursor = anchor(settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", person="Alice", recorded_at=cursor + 10.0)
    seed_event(state_db, "e2", person="Alice", recorded_at=cursor + 20.0)
    seed_event(state_db, "e3", person="Bob", recorded_at=cursor + 30.0)
    seed_event(state_db, "e4", person=None, recorded_at=cursor + 40.0)  # unknown stranger
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, settings, now=now)
    text = posted_text(http)
    assert "1 unknown visitor" in text
    assert "Known visitors: Alice (2), Bob (1)" in text


def test_known_visitors_are_excluded_by_default(state_db, settings_factory, http):
    """By default (SLACK_INCLUDE_KNOWN=false), recognized people never appear in Slack."""
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK, slack_include_known=False)
    now, cursor = anchor(settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", person="Alice", recorded_at=cursor + 10.0)
    seed_event(state_db, "e2", person=None, recorded_at=cursor + 20.0)
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, settings, now=now)
    text = posted_text(http)
    assert "1 unknown visitor" in text
    assert "Alice" not in text
    assert "Known visitors" not in text


def test_day_with_only_known_visitors_posts_when_enabled(state_db, settings_factory, http):
    """When only known people visited and SLACK_INCLUDE_KNOWN=true, summary posts."""
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK, slack_include_known=True)
    now, cursor = anchor(settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", person="Alice", recorded_at=cursor + 10.0)
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, settings, now=now)
    text = posted_text(http)
    assert "no unknown visitors" in text
    assert "Known visitors: Alice (1)" in text


def test_day_with_only_known_visitors_quiet_by_default(state_db, settings_factory, http):
    """When only known people visited and SLACK_INCLUDE_KNOWN=false, quiet day posts nothing."""
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK, slack_include_known=False, slack_summary_on_empty=False)
    now, cursor = anchor(settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", person="Alice", recorded_at=cursor + 10.0)
    rec.maybe_send_slack_summary(state_db, settings, now=now)
    assert len(http.calls) == 0


def test_known_person_names_are_slack_escaped(state_db, settings_factory, http):
    """Special characters in person names (<, >, &) must be escaped for Slack."""
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK, slack_include_known=True)
    now, cursor = anchor(settings)
    rec.set_meta(state_db, rec.SLACK_SENT_AT_KEY, str(cursor))
    seed_event(state_db, "e1", person="Alice <Sister & Friend>", recorded_at=cursor + 10.0)
    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    rec.maybe_send_slack_summary(state_db, settings, now=now)
    assert "Alice &lt;Sister &amp; Friend&gt; (1)" in posted_text(http)



# --------------------------------------------------------------------------
# Specific date selection (target_date)
# --------------------------------------------------------------------------

def test_slack_summary_for_specific_date(settings_factory, http):
    """Specifying a historical date selects events only from that calendar day and leaves cursor untouched."""
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK, slack_include_known=True)
    state = rec.open_state(settings.state_db)
    
    # 2026-08-19 in local time
    start_19, end_19, _ = rec.parse_date_window("2026-08-19")
    seed_event(state, "e_aug19", person="Alice", start=start_19 + 3600.0, end=start_19 + 3620.0, recorded_at=start_19 + 3600.0)
    
    # Another day (2026-08-20)
    start_20, _, _ = rec.parse_date_window("2026-08-20")
    seed_event(state, "e_aug20", person="Bob", start=start_20 + 3600.0, end=start_20 + 3620.0, recorded_at=start_20 + 3600.0)
    state.close()

    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    assert rec.slack_summary_now(settings, target_date="2026-08-19") == 0
    
    text = posted_text(http)
    assert "Alice" in text
    assert "Bob" not in text
    assert "19 Aug" in text

    # Cursor in state DB must NOT have been updated by historical query
    state = rec.open_state(settings.state_db)
    try:
        assert rec.get_meta(state, rec.SLACK_SENT_AT_KEY) is None
    finally:
        state.close()



def test_specific_date_refreshes_classification_before_unknown_filter(settings_factory, http):
    """Old state rows must use final source identity and face-presence metadata."""
    settings = settings_factory(
        dry_run=False,
        slack_webhook_url=WEBHOOK,
        slack_include_known=False,
        slack_unknown_requires_face=True,
    )
    start_19, _, _ = rec.parse_date_window("2026-08-19")
    build_source_db(settings.source_db, events=[
        make_event(id="e_known", start_time=start_19 + 60.0, end_time=start_19 + 80.0,
                   sub_label="Alice"),
        make_event(id="e_unknown", start_time=start_19 + 120.0, end_time=start_19 + 150.0,
                   sub_label=None,
                   data='{"attributes": [{"label": "face", "score": 0.88}]}'),
    ])
    state = rec.open_state(settings.state_db)
    try:
        seed_event(state, "e_known", start=start_19 + 60.0, end=start_19 + 80.0, person=None)
        seed_event(state, "e_unknown", start=start_19 + 120.0, end=start_19 + 150.0, person=None)
        state.execute("""INSERT INTO notion_delivery(event_id,page_id,synced_at,updated_at)
                         VALUES('e_known','alpha-page-id',?,?)""", (NOW, NOW))
        state.execute("""INSERT INTO notion_delivery(event_id,page_id,synced_at,updated_at)
                         VALUES('e_unknown','omega-page-id',?,?)""", (NOW, NOW))
        state.commit()
    finally:
        state.close()

    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    assert rec.slack_summary_now(settings, target_date="2026-08-19") == 0

    text = posted_text(http)
    assert "1 unknown visitor" in text
    assert "omegapageid" in text
    assert "alphapageid" not in text
    assert "Alice" not in text

    state = rec.open_state(settings.state_db)
    try:
        rows = {
            row["event_id"]: (row["person"], row["face_detected"])
            for row in state.execute(
                "SELECT event_id,person,face_detected FROM event_delivery")
        }
        assert rows == {
            "e_known": ("Alice", 0),
            "e_unknown": (None, 1),
        }
    finally:
        state.close()

def test_slack_people_summary_posts_known_people_for_specific_date(settings_factory, http):
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK)
    start_19, _, _ = rec.parse_date_window("2026-08-19")
    start_20, _, _ = rec.parse_date_window("2026-08-20")
    build_source_db(settings.source_db, events=[
        make_event(id="alice-1", start_time=start_19 + 60.0, end_time=start_19 + 80.0,
                   sub_label="Alice"),
        make_event(id="alice-2", start_time=start_19 + 120.0, end_time=start_19 + 150.0,
                   sub_label="Alice"),
        make_event(id="bob-1", start_time=start_19 + 180.0, end_time=start_19 + 210.0,
                   sub_label="Bob"),
        make_event(id="unknown-1", start_time=start_19 + 240.0, end_time=start_19 + 260.0,
                   sub_label=None),
        make_event(id="next-day", start_time=start_20 + 60.0, end_time=start_20 + 90.0,
                   sub_label="Carol"),
    ])

    http.add(responses.POST, WEBHOOK, body="ok", status=200)
    assert rec.slack_people_summary_now(settings, target_date="2026-08-19") == 0

    text = posted_text(http)
    assert "People in Hackerhouse" in text
    assert "1. Alice" in text
    assert "2. Bob" in text
    assert "Carol" not in text
    assert "Unknown Visitors" not in text
    assert "Events" not in text
    assert "First seen" not in text
    assert "Last seen" not in text


def test_slack_people_summary_invalid_date_format_fails(settings_factory, capsys):
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK)
    assert rec.slack_people_summary_now(settings, target_date="not-a-valid-date") == 1
    assert "Invalid date format" in capsys.readouterr().out


def test_slack_summary_invalid_date_format_fails(settings_factory, capsys):
    settings = settings_factory(dry_run=False, slack_webhook_url=WEBHOOK)
    assert rec.slack_summary_now(settings, target_date="not-a-valid-date") == 1
    assert "Invalid date format" in capsys.readouterr().out
