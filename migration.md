# Migration Plan

This is a reminder plan only. Do not treat it as an implementation checklist that is already coded.

Goal: move the door-camera automation off personal infrastructure and onto production S3 + production Notion without duplicating pages, losing clips, or deleting local recordings too early.

## Current constraints

- The Mac mini remains the active reconciler host.
- Fregata/Frigate SQLite and recordings remain the source of truth during migration.
- `reconciler-state.db` is important state. Do not delete it.
- Current state tracks uploaded S3 keys and Notion page ids, but it does not yet fully model multiple S3/Notion targets as separate destinations.
- Existing Notion `page_id` values point to the currently configured Notion database. Switching `NOTION_DATABASE_ID` alone can make old events look already synced and skip creating production pages.
- Existing Notion clip links may point to the old bucket until `clips-reset` and a refresh pass regenerate them.
- Current event HTML lists raw recording segments; planned Frigate API delivery will replace this with one generated MP4 for new/backfilled events.
- Frigate owns the local recording files. Do not add direct reconciler deletion until production migration and the single-clip architecture are complete; prefer Frigate retention.

## Phase 0 — Backups before touching production

Run these before changing `.env` on the Mac mini:

```bash
cp .env .env.backup.$(date +%Y%m%d-%H%M%S)
cp reconciler-state.db reconciler-state.db.backup.$(date +%Y%m%d-%H%M%S)
```

Also record current values privately:

- `S3_BUCKET`
- `S3_PREFIX`
- `AWS_REGION`
- `NOTION_DATABASE_ID`
- whether `CLIP_LINKS=true`
- whether `NOTION_INCLUDE_PERSON=true`

## Phase 1 — Production S3 bucket

Create/configure the production bucket first.

Required bucket posture:

- Block Public Access enabled.
- Same prefix convention as current bucket, normally `fregata`.
- Bucket region recorded exactly; `AWS_REGION` must match for presigned links.
- Lifecycle policy optional, but do not expire objects faster than the intended video retention period.

Upload identity policy should be scoped to the production prefix:

- `s3:PutObject`
- `s3:GetObject`
- `s3:AbortMultipartUpload`

Optional dedicated clip-signing identity:

- `s3:GetObject` only on the production prefix.
- Configure as `CLIP_AWS_ACCESS_KEY_ID` / `CLIP_AWS_SECRET_ACCESS_KEY`.
- This gives a kill switch for Notion clip links without disabling uploads.

## Phase 2 — Copy existing objects to production S3

Dry-run first:

```bash
aws s3 sync s3://<personal-bucket>/<prefix>/ s3://<production-bucket>/<prefix>/ --dryrun
```

Apply:

```bash
aws s3 sync s3://<personal-bucket>/<prefix>/ s3://<production-bucket>/<prefix>/
```

Verify object counts/bytes before switching the Mac:

```bash
aws s3 ls s3://<personal-bucket>/<prefix>/ --recursive --summarize
aws s3 ls s3://<production-bucket>/<prefix>/ --recursive --summarize
```

Do not delete personal-bucket objects yet. Keep them until production Notion links are verified.

## Phase 3 — Switch Mac mini to production S3

Update `.env`:

```env
S3_BUCKET=<production-bucket>
S3_PREFIX=fregata
AWS_REGION=<production-bucket-region>
AWS_ACCESS_KEY_ID=<production-upload-key>
AWS_SECRET_ACCESS_KEY=<production-upload-secret>

# optional, recommended for clip links
CLIP_AWS_ACCESS_KEY_ID=<production-readonly-signing-key>
CLIP_AWS_SECRET_ACCESS_KEY=<production-readonly-signing-secret>
```

Then run:

```bash
python3 reconciler.py once
python3 reconciler.py status
```

Expected:

- New uploads land in the production bucket.
- Existing state is not wiped.
- No new `events_failed` or clip signing failures.

Keep `CLIP_SOURCE=segments` during this transport-only cutover. The generated
single-event MP4 path is enabled later, after its API proof and state migration.

## Phase 4 — Production Notion preparation

Create or confirm the production Notion database.

Required properties:

- `Event ID` — title
- `Person` — select
- `Camera` — select
- `Seen` — date
- `Duration (s)` — number
- `Segments` — number
- `Manifest key` — rich text
- `Clip` — URL, if `CLIP_LINKS=true`
- `Score` — number, optional

Access requirements:

- Production Notion integration/token exists.
- Integration is shared with the production database.
- `test-notion.py` passes against production credentials before migration.

Privacy decision:

```env
NOTION_INCLUDE_PERSON=false
```

Keep false unless production Notion is explicitly allowed to store recognized names.

## Phase 5 — Single-event MP4 support before production Notion cutover

### Finding: Frigate Review items and reconciler HTML clips are different units

The Frigate UI shows high-level Review activity windows. The current reconciler
does not export those Review clips. It selects every raw row from the
`recordings` table that overlaps:

```text
event.start_time - PRE_ROLL_SECONDS
through
event.end_time + POST_ROLL_SECONDS
```

It uploads each raw recording segment and renders one `<video>` element per
segment in the event HTML page. Therefore two Review items in Frigate can
legitimately correspond to four or more raw videos in the HTML. Boundary
segments can contain only pre-roll or post-roll and show no visible person.

Frigate documents a direct recording-clip endpoint that returns one MP4 for a
camera and time range:

```http
GET /api/<camera_name>/start/<start_ts>/end/<end_ts>/clip.mp4
```

Reference:
https://docs.frigate.video/integrations/api/recording-clip-camera-name-start-start-ts-end-end-ts-clip-mp-4-get/

Use this direct endpoint for automation instead of the asynchronous export API.
It avoids export-job polling and produces the single media object needed by
Notion.

Important non-goal: the API returns one video for the requested time range. It
does not trim away every frame where the person is absent. Existing
`PRE_ROLL_SECONDS` and `POST_ROLL_SECONDS` still control surrounding context.

### Manual API proof before implementation

Choose a known event whose existing HTML has multiple videos:

```bash
DB=\"$HOME/Fregata/config/frigate.db\"
EVENT_ID=\"<event-id>\"

sqlite3 -readonly -header -column \"$DB\" \"
SELECT
  id,
  camera,
  start_time,
  end_time,
  datetime(start_time, 'unixepoch', 'localtime') AS start_local,
  datetime(end_time, 'unixepoch', 'localtime') AS end_local
FROM event
WHERE id = '$EVENT_ID';
\"
```

Then request the same padded range from the local Frigate/Fregata API:

```bash
CAMERA=\"door_camera\"
START=\"<event-start-minus-pre-roll>\"
END=\"<event-end-plus-post-roll>\"

curl -fL \
  \"http://127.0.0.1:5000/api/$CAMERA/start/$START/end/$END/clip.mp4\" \
  -o /tmp/frigate-event-test.mp4

open /tmp/frigate-event-test.mp4
```

The actual base URL, port, TLS, and authentication must be established on the
Mac mini before coding. Do not assume port 5000 if Fregata exposes the API
through another local endpoint.

Proof acceptance:

- Response is HTTP 200 and a non-empty playable MP4.
- It contains the intended person event.
- It is one coherent video rather than multiple HTML players.
- It can be fetched non-interactively from the reconciler process.

### Target event flow

```text
person event
  -> compute padded start/end
  -> download one MP4 from Frigate recording-clip API
  -> upload one production S3 object
  -> store generated-clip delivery state
  -> put one presigned video link in production Notion
```

Target S3 key:

```text
fregata/events/<camera>/<event_id>/clip.mp4
```

The temporary local MP4 must be removed in `finally` after upload or failure.
It is not a second permanent local recording.

### Configuration and rollout flag

Add:

```env
CLIP_SOURCE=segments
FRIGATE_API_URL=http://127.0.0.1:5000/api
FRIGATE_API_TIMEOUT_SECONDS=120
```

Supported sources:

```text
segments     current raw-segment + multi-video HTML behavior
frigate_api  one generated event MP4
```

Ship with `CLIP_SOURCE=segments`. Switch the Mac mini to
`CLIP_SOURCE=frigate_api` only after the manual API proof and automated tests
pass.

Do not silently fall back to raw segments on an API error during initial
rollout. A silent fallback would reintroduce multi-video pages while appearing
successful. Fail the event visibly and retry on the next reconciliation pass.
Legacy events that already have segment delivery state remain readable.

### Separate generated-clip delivery state

Do not overload `segment_delivery`; those rows mean raw Frigate recording
segments. Add:

```sql
CREATE TABLE IF NOT EXISTS event_clip_delivery (
  event_id TEXT NOT NULL,
  bucket TEXT NOT NULL,
  endpoint_url TEXT NOT NULL DEFAULT '',
  region TEXT NOT NULL,
  source TEXT NOT NULL,
  camera TEXT NOT NULL,
  start_time REAL NOT NULL,
  end_time REAL NOT NULL,
  s3_key TEXT,
  etag TEXT,
  size_bytes INTEGER,
  generated_at REAL,
  uploaded_at REAL,
  last_error TEXT,
  updated_at REAL NOT NULL,
  PRIMARY KEY(event_id, bucket, endpoint_url)
);
```

Idempotency and destination rules:

- Read/write the row matching the currently configured bucket and endpoint.
- `uploaded_at IS NOT NULL` means media generation/upload to that destination is complete.
- An interrupted download leaves no success state and retries.
- An upload followed by a state-write crash safely overwrites the same S3 key.
- A completed legacy `event_delivery` row must not block the separate backfill
  command described below.

### Download and upload contract

Implement a streamed download; do not buffer a complete video in memory.

Required checks:

- HTTP status is 200.
- Response body is non-empty.
- Temporary file is fully written before S3 upload.
- S3 `head_object` succeeds after upload.
- S3 `ContentLength` matches the generated file size.
- Temporary file is removed on every path.

`Content-Type: video/mp4` is expected but should not be the only validity check
because proxies may omit or rewrite it.

### Notion clip behavior

Change clip refresh precedence:

1. If the current production destination's
   `event_clip_delivery.uploaded_at` exists, presign that one MP4 and put it in
   the Notion `Clip` property.
2. Otherwise, keep the legacy segment HTML path for already-delivered events.

This keeps historical pages usable while all new events move to one video. A
direct MP4 link is preferred. If Safari/iOS behavior is inadequate in live
testing, retain a one-video HTML page rather than reverting to multiple
segments.

`clips-reset` remains a link-state reset only. It must not generate media.

### Historical backfill

Existing completed events are skipped by `process_event`, so add an explicit,
resumable command:

```bash
python3 reconciler.py event-clips-backfill --date <YYYY-MM-DD> --dry-run
python3 reconciler.py event-clips-backfill --date <YYYY-MM-DD> --apply
```

Behavior:

- Select historical person events with no successful `event_clip_delivery`.
- Use saved event start/end plus configured padding.
- Generate and upload the MP4 to the currently configured bucket.
- Set the active production Notion target's `clip_signed_at` to `NULL`.
- Never recreate Notion pages or re-upload raw segments.
- Report created, skipped, unavailable-source, and failed counts.

Backfill depends on Frigate still having the source recordings. If retention
already removed them, preserve that event's legacy segment HTML; do not fail the
whole migration.

Run backfill against the production bucket after the S3 cutover. Do not create
new event MP4s in the personal bucket merely to copy them again.

### Manifest compatibility

Use a versioned manifest for generated clips:

```json
{
  \"schema_version\": 2,
  \"source\": \"fregata-sqlite-reconciler\",
  \"event\": {},
  \"archive_window\": {},
  \"clip\": {
    \"source\": \"frigate_api\",
    \"s3_key\": \"fregata/events/door_camera/<event_id>/clip.mp4\",
    \"size_bytes\": 1234567,
    \"etag\": \"...\"
  },
  \"segments\": []
}
```

Keep `segments` present for compatibility. For the existing Notion `Segments`
number property, write `1` for a generated event MP4 unless the production
database adopts a clearer replacement property.

### Required tests

- `CLIP_SOURCE=segments` preserves current behavior.
- `CLIP_SOURCE=frigate_api` makes one API request and uploads one MP4.
- Generated upload is idempotent.
- HTTP 404/500, timeout, and empty response do not mark delivery complete.
- Temporary files are removed after success and failure.
- S3 length mismatch fails delivery.
- Notion clip refresh prefers the generated MP4.
- Notion clip refresh falls back to legacy segment HTML.
- `clips-reset` changes link state only.
- Backfill dry-run performs no writes.
- Backfill apply processes missing clips and resumes after interruption.
- Missing historical source recordings leave legacy links intact.

### Local cleanup consequence

With `CLIP_SOURCE=frigate_api`, raw recording files remain owned by Frigate.
The reconciler should not unlink them directly after creating an event MP4;
multiple events and Review items can share those source segments, and Frigate's
SQLite database would otherwise retain paths to missing files.

Preferred local cleanup:

- Let Frigate's `record.continuous`, `record.motion`, `record.alerts`, and
  `record.detections` retention policies delete raw recordings.
- Delete the reconciler's generated temporary MP4 immediately after S3 upload.
- Use prefix-specific S3 lifecycle rules only after production Notion links are
  verified.

Revisit any proposed `cleanup-local` command after the single-clip cutover. It
must not directly delete Frigate-owned raw segments by default.

## Phase 6 — Required target-aware Notion support before cutover

Before switching production Notion live, add target-aware Notion migration support.

Needed command:

```bash
python3 reconciler.py notion-migrate --dry-run
python3 reconciler.py notion-migrate --apply
```

Expected command behavior:

- Reads delivered events from `reconciler-state.db`.
- Uses `Event ID` as the stable dedupe key.
- Searches the configured production Notion database for each event.
- If a page exists, records that production `page_id`.
- If missing, creates the production page.
- Does not modify personal Notion pages.
- Does not re-upload S3 objects.
- Is resumable after interruption.
- Rate-limits Notion writes.
- Marks production clip state stale so clip links are regenerated against production S3.

State model should become target-aware, e.g.:

```sql
notion_delivery (
  event_id TEXT,
  notion_database_id TEXT,
  page_id TEXT,
  synced_at REAL,
  last_error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  clip_signed_at REAL,
  clip_attempts INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY(event_id, notion_database_id)
)
```

Migration rule:

- Existing rows are assigned to the old/current Notion database id.
- Production rows live beside old rows.
- `sync_notion`, `refresh_clip_links`, Slack Notion links, and `status` must read rows for the currently configured database only.

Do not rely on just changing `NOTION_DATABASE_ID`; that can silently skip historical events.

## Phase 7 — Production dry-runs

After both single-event clip support and target-aware Notion support exist,
update `.env` to production targets:

```env
S3_BUCKET=<production-bucket>
AWS_REGION=<production-bucket-region>
NOTION_TOKEN=<production-token>
NOTION_DATABASE_ID=<production-database-id>

# Keep legacy delivery active until the generated-clip proof below succeeds.
CLIP_SOURCE=segments
FRIGATE_API_URL=<verified-local-api-base>/api
```

Run:

```bash
python3 test-notion.py
python3 reconciler.py notion-migrate --dry-run
python3 reconciler.py event-clips-backfill --date 2026-08-29 --dry-run
```

The Notion dry-run should report:

```text
Would create: <n> pages
Would link existing: <n> pages
Would update state only: <n> pages
Would skip: <n> pages
Errors: 0
```

The clip dry-run should report:

```text
Would generate: <n> event clips
Already generated: <n>
Source recordings unavailable: <n>
Errors: 0
```

No production media or Notion page writes should happen during either dry-run.
`test-notion.py` is the only exception if it is explicitly designed to create a
test page.

## Phase 8 — Generate single-event clips in production S3

Generate one representative historical clip first:

```bash
python3 reconciler.py event-clips-backfill --date 2026-08-29 --apply
```

Verify:

- One object exists at
  `fregata/events/<camera>/<event_id>/clip.mp4` in production S3.
- Its size matches `event_clip_delivery.size_bytes`.
- A presigned GET returns HTTP 200 and `video/mp4`.
- The video is playable and corresponds to the intended Frigate event.
- It is one coherent MP4 even when the old HTML contained several raw segments.

After that proof, switch new event delivery:

```env
CLIP_SOURCE=frigate_api
```

Then:

```bash
python3 reconciler.py once
python3 reconciler.py status
```

Backfill other historical dates only while their Frigate source recordings
still exist. Missing-source events retain their legacy multi-segment HTML:

```bash
python3 reconciler.py event-clips-backfill --date <YYYY-MM-DD> --dry-run
python3 reconciler.py event-clips-backfill --date <YYYY-MM-DD> --apply
```

Do not run `clips-reset` yet. Production Notion migration must establish the
production page ids first.

## Phase 9 — Apply production Notion migration

Run:

```bash
python3 reconciler.py notion-migrate --apply
python3 reconciler.py clips-reset
python3 reconciler.py once
python3 reconciler.py status
```

Expected:

- Production Notion pages exist for historical delivered events.
- New production page ids are stored in target-aware local state.
- Events with generated clips link directly to one production S3 MP4.
- Events that could not be backfilled retain their legacy segment HTML.
- Personal Notion pages are untouched.
- Slack unknown summaries link to production Notion pages.

Manual verification:

- Open a new production Notion page and confirm `Clip` opens one video.
- Open a legacy event that could not be backfilled and confirm its old HTML
  still works.
- Confirm a Slack unknown visitor summary links to production Notion, not
  personal Notion.
- Confirm no fresh writes go to the personal S3 bucket.

## Phase 10 — Stabilization window

Wait a few normal operating days before changing retention or deleting legacy
objects.

During this window:

```bash
python3 reconciler.py status
python3 reconciler.py slack-summary <known-test-date>
python3 reconciler.py slack-people-summary <known-test-date>
```

Expected:

- New person events create exactly one generated S3 MP4.
- No Frigate API download, S3 upload, or Notion auth/schema errors.
- Clip links continue to refresh.
- Legacy pages remain usable where backfill was impossible.
- Slack summary still shows unknown-only events.
- People summary remains a plain numbered name list.

## Phase 11 — Retention and legacy cleanup, later

The single-event architecture changes the local-cleanup decision. Raw files
under `FREGATA_RECORDINGS_DIR` are Frigate-owned inputs and can be shared by
multiple events and Review items. The reconciler should not unlink those files
after uploading one generated event MP4.

Use Frigate retention for local media:

```yaml
record:
  enabled: true
  continuous:
    days: <local-continuous-retention>
  motion:
    days: <local-motion-retention>
  alerts:
    retain:
      days: <local-alert-retention>
  detections:
    retain:
      days: <local-detection-retention>
```

Generated temporary MP4 files are deleted immediately after each S3 upload.
They do not need a scheduled cleanup job.

After the stabilization window, add prefix-specific production S3 lifecycle
rules:

- `fregata/events/*/clip.mp4`: retain according to the production event-video
  policy.
- `fregata/recordings/`: retain legacy raw segments until every required
  historical Notion page either has a generated clip or is accepted as legacy.
- `fregata/slack-snapshots/`: use a shorter privacy-appropriate retention.
- Legacy `events/*/index.html`: retain until old presigned URLs have expired and
  legacy pages are no longer required.

Before expiring legacy raw S3 objects, produce a report of:

- generated-clip events,
- legacy-HTML events,
- events whose Frigate source recordings are gone,
- production Notion pages still pointing at legacy HTML.

Only delete/expire a legacy prefix after that report is reviewed. Do not
implement a general `cleanup-local --apply` that deletes Frigate-owned raw
segments as part of this migration.

## Rollback plan

If production S3 fails:

- Restore old `.env` S3 values.
- Run `python3 reconciler.py once`.
- Run `python3 reconciler.py status`.
- Do not delete production objects until root cause is known.

If production Notion fails:

- Restore old `.env` Notion values.
- Run `python3 reconciler.py clips-reset` only if old links need repair.
- Keep production pages already created; do not mass-delete during incident response.
- Fix schema/token/share access, then rerun migration dry-run.

If Frigate single-clip generation fails:

- Set `CLIP_SOURCE=segments` to restore the legacy upload path for new events.
- Keep `event_clip_delivery` rows and production MP4 objects for diagnosis; do
  not delete successful output during rollback.
- Run `python3 reconciler.py clips-reset` only after choosing which media source
  production Notion should link to.
- Verify the local Frigate API base URL, authentication, source recording
  availability, timeout, and returned MP4 before retrying backfill.

If clip links fail after cutover:

- Check `AWS_REGION` matches the production bucket region.
- Check signing identity has `s3:GetObject` on the production prefix.
- Run `python3 reconciler.py clips-reset` after credentials are fixed.

## Final acceptance criteria

Migration is complete only when:

- New S3 uploads land in production.
- New person events produce one generated MP4 in production S3.
- Historical events are backfilled while Frigate source recordings remain available.
- Events that cannot be backfilled retain a documented, working legacy HTML path.
- Production Notion has pages for delivered historical events.
- Production Notion `Clip` links prefer generated MP4s and open successfully.
- Slack unknown summaries link to production Notion.
- Personal AWS credentials are no longer used by the Mac mini.
- Personal Notion credentials are no longer used by the Mac mini.
- Frigate retention, not direct reconciler file deletion, owns local raw-recording cleanup.
- Legacy S3 prefixes remain until the stabilization report is reviewed.
