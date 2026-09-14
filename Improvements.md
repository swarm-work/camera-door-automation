# Improvements Plan

Planning document only. None of the features below should be treated as implemented or enabled.

## Goals

1. Improve face-library quality and eventually filter automation to events with real faces.
2. Send a delta digest every two hours, while retaining a separate end-of-day report.
3. Prevent audio from creating motion-retained clips when that behavior is not wanted.
4. Archive and eventually delete S3 footage with explicit retention rules.
5. Keep scheduling reliable on the Mac mini, including sleep, restart, retry, and catch-up behavior.

## 1. Face-only event processing

### Repository reviewed

Candidate companion:

- [mayerwin/frigate-better-face-recognition](https://github.com/mayerwin/frigate-better-face-recognition)

Frigate references:

- [Frigate face recognition](https://docs.frigate.video/configuration/face_recognition/)
- [Frigate audio detection](https://docs.frigate.video/configuration/audio_detectors/)
- [Frigate review items](https://docs.frigate.video/configuration/review/)

### Verdict

`frigate-better-face-recognition` is useful as a face-library cleanup and training companion. It is not a switch that makes Frigate record only when a face exists.

Frigate first detects a `person`, then looks for and recognizes a face inside that person detection. A recognized name is stored as the person's `sub_label`. Therefore:

```text
person detected -> face attempted -> optionally recognized -> sub_label assigned
```

The current reconciler filters Frigate/Fregata events by `label=person`. It does not prove that a usable face was present. A person may be detected from behind, at a distance, or with no visible face.

### What the companion adds

The companion polls Frigate's face training crops through `/api/faces`, then applies:

- SCRFD to verify that a crop contains a real face.
- ArcFace embeddings to compare it with enrolled people.
- eDifFIQA to score whether the face image is useful for recognition.
- A human review queue for assigning a crop to a person or marking it as not a face.
- Optional writeback that trains or deletes crops in Frigate.

It can reduce bad training data such as wheels, walls, partial faces, very blurry crops, and repeated look-alike junk. It runs locally and stores its people, embeddings, decisions, and thumbnails in `/app/data/bfr.db`.

### Limits and risks

- It was written against Frigate v0.17.1 face APIs. Compatibility with the current Fregata build is not established.
- It does not suppress Frigate person, audio, or Review items.
- Its default background ingestion runs while its Review page is open. It also runs continuously when auto-label is enabled, but auto-label writes names without human confirmation and should remain off initially.
- It builds from source; no prebuilt image is published.
- Active model use needs roughly 0.8-1 GB RAM and CPU inference time.
- Frigate's internal port `5000` is unauthenticated. It must remain on a private container/host network.
- The optional Frigate UI button requires mounting `/var/run/docker.sock`, which gives the container powerful host access. Start with `NGINX_AUTOCONFIG=false` and do not mount the Docker socket.
- Face images and embeddings are biometric data. Port `8975` must not be publicly exposed, and `bfr.db` must be protected and backed up.

### Compatibility preflight

Before deploying the companion, verify the current Fregata instance has the required API:

```bash
curl -fsS http://127.0.0.1:5000/api/version
curl -fsS http://127.0.0.1:5000/api/faces
```

The second command must return the face folders, including the `train` queue. If either endpoint is absent or incompatible, do not deploy this companion against Fregata.

### Safe trial configuration

Start read-only from the companion's perspective:

```yaml
services:
  better-face-recognition:
    build: https://github.com/mayerwin/frigate-better-face-recognition.git
    container_name: better-face-recognition
    restart: unless-stopped
    environment:
      FRIGATE_URL: "http://frigate:5000"
      AUTH: "frigate"
      NGINX_AUTOCONFIG: "false"
      FRIGATE_WRITEBACK: "false"
      AUTO_LABEL: "false"
    volumes:
      - ./bfr-data:/app/data
    ports:
      - "127.0.0.1:8975:8975"
    networks:
      - frigate_default
```

Do not enable writeback until its real-face and quality decisions have been manually reviewed. If writeback is later enabled, keep `AUTO_LABEL=false` until false matches have been measured and accepted.

### Future reconciler filter modes

The reconciler should own the final rule for which events reach S3, Notion, and Slack. Proposed explicit modes:

```env
EVENT_FILTER=person
# Future alternatives:
# EVENT_FILTER=recognized_face
# EVENT_FILTER=face_attempt
# EVENT_FILTER=verified_face
```

Definitions:

| Mode | Meaning | Tradeoff |
| --- | --- | --- |
| `person` | Every Frigate event with `label=person`; current behavior | Includes people whose face was not visible |
| `recognized_face` | `label=person` and `sub_label` is populated | Drops unknown visitors entirely |
| `face_attempt` | Person event has a corresponding Frigate face crop | Includes unknown faces, but may include junk crops |
| `verified_face` | Face crop is independently confirmed as a real face | Best match for the requested behavior, but requires a durable verification signal |

Recommended eventual target: `verified_face`, with unknown real faces retained and non-face person detections ignored. Do not use `sub_label IS NOT NULL` as a substitute, because that would exclude unknown visitors.

## 2. Configurable interval digests and daily summary

### Message contracts

Use distinct messages and independent cursors.

#### Interval digest

Purpose: operational visibility during the day. The initial cadence is two hours,
but it must be configuration rather than a hard-coded schedule.

- Contains only new eligible events since the previous successful interval post.
- Uses the future face filter; until then it uses the current unknown-person event set.
- Does not include recognized household names in the default message.
- Does not send an empty message each interval unless a health warning exists.
- On failure, does not advance its cursor; retry on the next watch pass.
- If the Mac sleeps through a boundary, send one catch-up digest on wake covering the complete gap.

State key:

```text
slack_interval_sent_at
```

A separate key is required; the existing daily summary cursor must not be reused.

#### End-of-day summary

Purpose: complete daily rollup rather than an alert feed.

- Covers one complete local calendar day.
- Repeats events already seen in interval digests intentionally, but labels the message as a daily rollup.
- Includes health/proof-of-life data so zero events is distinguishable from an outage.
- Keeps the simple `People in Hackerhouse` numbered-name report separate from unknown-event reporting.
- Uses a daily date marker, not the interval cursor.

Recommended schedule: run at `00:05` and summarize the previous local calendar day. A report sent at `21:00` is not a whole-day report because events between 21:00 and midnight are absent. The five-minute delay also lets final Frigate events settle.

State key:

```text
slack_daily_last_date
```

### Proposed configuration

Reuse the existing daily settings rather than creating a second naming convention:

```env
SLACK_INTERVAL_ENABLED=false
SLACK_INTERVAL_HOURS=2
SLACK_SUMMARY_TIME=00:05
SLACK_SUMMARY_ON_EMPTY=true
```

Changing the interval should require only an environment update and service
restart, for example `SLACK_INTERVAL_HOURS=5`. Validate it as a positive number.
The scheduler must measure from its last successful cursor rather than assume
the value divides evenly into 24 hours.

All new outbound-message flags must default to false until explicitly enabled.

### Proposed manual commands

These commands are planning targets, not currently implemented:

```bash
python3 reconciler.py slack-interval-summary
python3 reconciler.py slack-summary --date 2026-08-31
python3 reconciler.py slack-health
```

Manual historical commands must not advance automated cursors.

### Boundary and deduplication rules

- Store timestamps as UTC epochs; calculate schedule boundaries in the Mac's configured local timezone.
- Query half-open windows, `[since, until)`, so an event on a boundary appears exactly once.
- Advance a cursor only after Slack returns success.
- Initialize a new interval cursor at enable time; do not dump all historical events.
- Use separate idempotency state for interval and daily messages.
- Changing `SLACK_INTERVAL_HOURS` preserves the existing cursor and starts the new cadence from the last successful post.
- Cap Slack Block Kit rows and link to the production Notion database for overflow.
- Do not run two schedulers that can post the same cursor concurrently.

## 3. Scheduling on the Mac mini

### Recommended automation: existing launchd watch service

Do not add cron for the primary Mac deployment. Keep the existing launchd service running:

```text
launchd/com.swarm.entry-logger.plist
  -> python3 reconciler.py watch
```
The `watch` loop already polls continuously and currently checks whether the daily Slack summary is due. Extend that same process to check both schedules:

```text
every poll:
  reconcile Frigate events to S3/Notion
  if last successful interval post is at least SLACK_INTERVAL_HOURS old:
    send one gap-free interval digest
  if the previous local day has not received its EOD report:
    send the complete previous-day summary
  record heartbeat/health state
  sleep POLL_SECONDS
```

Benefits:

- One process owns the SQLite cursors, avoiding duplicate-post races.
- A sleeping Mac catches up when it wakes.
- `KeepAlive` restarts the process after failure.
- `RunAtLoad` starts it after login/reboot.
- Existing stdout/stderr log locations continue to apply.

The only production process command should remain:

```bash
cd /Users/swarm/ws/camera-door-automation
source .venv/bin/activate
python3 reconciler.py watch
```

In launchd, use absolute paths; it does not activate a shell virtual environment. The plist should directly call:

```text
/Users/swarm/ws/camera-door-automation/.venv/bin/python3
/Users/swarm/ws/camera-door-automation/reconciler.py
watch
```

This single launchd-managed process is the required continuously running EOD
automation. Do not create a second EOD daemon: separate processes can race over
the same SQLite state and post duplicates.

### Why not cron

- Cron jobs can be missed while the Mac is asleep.
- Separate interval and daily processes can race over the same state DB/cursor.
- Cron has a minimal environment and frequently misses `.env`, PATH, or virtualenv assumptions.
- The existing long-running launchd service already provides restart and catch-up behavior.

### Linux/native Frigate alternative later

If the reconciler eventually moves to Linux and no watch service is used, equivalent cron expressions would be:

```cron
0 */2 * * * cd /opt/camera-door-automation && .venv/bin/python3 reconciler.py slack-interval-summary
5 0 * * * cd /opt/camera-door-automation && .venv/bin/python3 reconciler.py slack-summary --date "$(date -d yesterday +\%F)"
```

These are illustrative future commands only. The commands and process-level locking must exist before installing these entries. On Linux, a systemd service/timer is preferable to cron for logging, retries, dependency ordering, and missed-run handling.

## 4. Slack thumbnail durability

### Why old thumbnails disappear

The current Slack card does not upload the snapshot into Slack. It uploads the
image to S3 and places a presigned `image_url` in the Block Kit message. Slack
requires `image_url` to be publicly reachable when it fetches or re-fetches the
message image. See Slack's [image element reference](https://docs.slack.dev/reference/block-kit/block-elements/image-element/).

The reconciler signs snapshot URLs with `CLIP_URL_TTL_SECONDS`. AWS SigV4 permits
at most seven days, and temporary SSO/STS credentials shorten that further: the
URL stops working when the signing session expires. Slack can then no longer
retrieve the image even though the snapshot object still exists in S3. This is
expected with the current design and explains why previously visible thumbnails
later become blank.

### Product decision required

Choose one explicit retention contract:

1. **Expiring thumbnail, recommended privacy default.** Keep the current
   presigned URL design, state in the message/docs that thumbnails are temporary,
   and use the Notion link for later viewing.
2. **Durable Slack-hosted image.** Add a Slack bot token and upload snapshots with
   Slack's file API, then reference a `slack_file` ID in Block Kit. This persists
   according to Slack workspace retention, but creates a second biometric-image
   store and requires `files:write`, file deletion, and retention controls.
3. **Refresh old Slack messages.** Store channel/message timestamps and use
   `chat.update` to replace expired URLs before they lapse. Incoming webhooks are
   insufficient; this needs a bot token, message ownership, retry state, and a
   permanent scheduler.

Never make the S3 snapshot prefix public merely to keep Slack thumbnails alive.

### Proposed configuration

```env
SLACK_SNAPSHOT_MODE=expiring_url
SLACK_SNAPSHOT_URL_TTL_SECONDS=604800
# Future durable mode:
# SLACK_SNAPSHOT_MODE=slack_file
```

Give snapshots their own TTL setting rather than implicitly sharing the Notion
clip TTL. Clamp presigned URLs to the AWS seven-day maximum and report when
temporary credentials reduce the real lifetime.

### Implementation tasks

- Decide whether historical Slack thumbnails are intentionally temporary.
- Add the separate snapshot TTL and startup validation.
- Include an expiry note in messages when using presigned URLs.
- If durable mode is approved, add Slack bot authentication and file uploads;
  do not reuse the incoming webhook as though it can upload binary files.
- Store Slack file IDs and message timestamps needed for deletion/audit.
- Add a Slack-file retention cleanup that matches company policy.
- Keep S3 snapshot lifecycle longer than the promised URL lifetime plus a small
  retry buffer.
- Test that expired URLs fail without affecting the text summary or Notion link.

## 5. Prevent sound from retaining motion clips

Frigate's [audio detector documentation](https://docs.frigate.video/configuration/audio_detectors/) states that audio volume above `min_volume` is considered motion for recording retention. Audio events also save a snapshot and recordings for the duration of the event.

Fregata may expose a Frigate-compatible but version-specific configuration
schema. Validate these fields in its config editor before restart; do not copy a
current Frigate example over the complete existing Fregata configuration.

There are two separate decisions:

1. Should sound trigger detection/retention?
2. Should saved video files contain an audio track?

They are configured separately.

### Recommended: sound does not trigger clips, but recordings may retain audio

Disable audio detection globally or on the door camera and remove the `audio` input role. Keep an audio-capable record preset only if audio should remain inside videos:

```yaml
audio:
  enabled: false

cameras:
  door_camera:
    audio:
      enabled: false
    ffmpeg:
      inputs:
        - path: rtsp://<camera-stream-with-video-and-audio>
          roles:
            - detect
            - record
      output_args:
        record: preset-record-generic-audio-copy
```

Use `preset-record-generic-audio-aac` instead if the camera's audio codec is not directly compatible and must be transcoded. Frigate documents these presets in [FFmpeg presets](https://docs.frigate.video/configuration/ffmpeg_presets/).

With audio detection disabled and no `audio` role, audio should not generate audio events or count as motion. The record stream may still contain an audio track.

### Strictest option: neither sound-triggering nor audio in files

```yaml
audio:
  enabled: false

cameras:
  door_camera:
    audio:
      enabled: false
    ffmpeg:
      inputs:
        - path: rtsp://<camera-stream>
          roles:
            - detect
            - record
      output_args:
        record: preset-record-generic
```

`preset-record-generic` records without audio.

### Remove audio labels from Review configuration

If present, remove `speech`, `bark`, `yell`, `scream`, `fire_alarm`, and other
audio labels from `review.alerts.labels` and `review.detections.labels`. Preserve
every wanted non-audio object label. For a camera intended to review people only,
the relevant part can remain:

```yaml
review:
  alerts:
    labels:
      - person
```

Disabling the audio detector is the primary control. Cleaning the Review lists
prevents a future audio configuration change from unexpectedly restoring audio
Review items.

### Change procedure

1. Back up the current Frigate/Fregata config.
2. Inspect global `audio`, per-camera `audio`, FFmpeg input roles, recording output preset, and Review label lists.
3. Apply one of the configurations above through the Frigate config editor or the actual mounted config file.
4. Validate the configuration in Frigate before restarting.
5. Restart Frigate/Fregata.
6. Generate a controlled sound with no person in frame.
7. Confirm no audio-only Review item is created.
8. Confirm the camera still records video as intended.
9. If audio was retained in recordings, open a recording and confirm its audio track remains.

### Database verification

After the test, check for audio Review items:

```bash
sqlite3 -readonly "$HOME/Fregata/config/frigate.db" "
SELECT
  id,
  camera,
  datetime(start_time, 'unixepoch', 'localtime') AS start_local,
  json_extract(data, '$.objects') AS objects,
  json_extract(data, '$.audio') AS audio
FROM reviewsegment
WHERE json_extract(data, '$.audio') IS NOT NULL
  AND json_extract(data, '$.audio') != '[]'
ORDER BY start_time DESC
LIMIT 20;
"
```

Old rows remain in the database; verify that no new row appears after the configuration-change timestamp.

Also verify person events independently:

```bash
sqlite3 -readonly "$HOME/Fregata/config/frigate.db" "
SELECT
  id,
  label,
  sub_label,
  datetime(start_time, 'unixepoch', 'localtime') AS start_local
FROM event
WHERE label='person'
ORDER BY start_time DESC
LIMIT 20;
"
```

## 6. S3 clip archiving and deletion

### Use S3 Lifecycle, not a cleanup cron

Cloud retention belongs in an S3 Lifecycle policy. Do not run a Mac cron job that lists and deletes S3 objects. Lifecycle rules continue working while the Mac is offline, are auditable in AWS, and avoid partial client-side deletion loops.

Current object families include:

| Prefix/object | Purpose | Recommended policy |
| --- | --- | --- |
| `fregata/recordings/` | Video segments used by clip viewer pages | Hot storage, then archive, then expire |
| `fregata/slack-snapshots/` | Presigned images embedded in Slack | Expire quickly; do not archive |
| `fregata/events/.../index.html` | Presigned viewer page | Keep hot while videos are viewable; expire with or shortly after videos |
| `fregata/events/.../manifest.json` | Small event metadata, when enabled | Keep longer than video or retain for audit |

### Proposed default retention

Final durations require production owner/security approval. A reasonable initial policy is:

| Artifact | Standard/hot | Archive | Delete |
| --- | ---: | ---: | ---: |
| Recording segments | 30 days | Glacier Instant Retrieval on day 30 | Day 180 |
| Slack snapshots | 14 days | Never | Day 14 |
| Viewer HTML | 30 days | Never | Day 181 |
| Event manifests | 365 days | Optional | Day 365 or retain indefinitely |
| Incomplete multipart uploads | N/A | N/A | Abort after 7 days |

Why Glacier Instant Retrieval: Notion links can still perform an immediate GET, though retrieval charges apply. Glacier Flexible Retrieval and Deep Archive require restoration before playback, so current presigned clip links would not work immediately.

AWS minimum-storage-duration charges must be included in the decision. Glacier Instant Retrieval and Flexible Retrieval have 90-day minimums; Deep Archive has a 180-day minimum. See [AWS lifecycle transition considerations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-transition-general-considerations.html) and [Glacier storage classes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/glacier-storage-classes.html).

### Required application coordination

Lifecycle expiration alone is not enough. Before enabling it:

- Decide the user-visible clip retention promise.
- Stop refreshing Notion viewer links after their recording objects have expired.
- Mark an expired clip in Notion as unavailable instead of leaving a viewer whose videos return 404.
- Ensure Slack snapshots expire only after their presigned URLs are no longer expected to work.
- Use object tags for artifact types that share the `fregata/events/` prefix, or move future viewer pages/manifests into separate lifecycle-friendly prefixes.
- If bucket versioning is enabled, add expiration for noncurrent versions and expired delete markers; otherwise old versions continue costing storage.
- Preserve `reconciler-state.db`; S3 expiration must not trigger re-upload loops.

Suggested object tags for future uploads:

```text
artifact=recording
artifact=slack-snapshot
artifact=viewer
artifact=manifest
```

### Lifecycle rollout

1. Complete the production S3 migration in `migration.md`.
2. Measure current object counts, average sizes, and monthly retrieval expectations.
3. Agree retention durations with the production owner.
4. Add application handling for expired clips and lifecycle-aware Notion links.
5. Create the lifecycle policy in a non-production/test prefix first.
6. Confirm transition and expiration dates using S3 object metadata and Lifecycle rule status.
7. Apply the policy to production prefixes/tags.
8. Add cost and object-count monitoring.
9. Keep the personal bucket unchanged until production playback and expiration behavior are verified.

### Local file deletion is separate

Deleting local Frigate recordings after successful S3 upload is a different control. Its future implementation must:

- remain disabled by default;
- wait a safety period, recommended seven days;
- restrict deletion to `FREGATA_RECORDINGS_DIR`;
- verify the object in the currently configured production bucket with `HeadObject`;
- compare S3 `ContentLength` with local file size;
- record `local_deleted_at` or an error in SQLite;
- run dry-run before apply.

Do not enable local deletion during the S3 or Notion production migration.

## 7. Recommended implementation order

1. Disable audio-triggered motion retention and verify it with a controlled test.
2. Add reconciler heartbeat and Frigate recording-freshness health reporting.
3. Define and implement the durable face-evidence signal.
4. Choose the Slack thumbnail retention contract.
5. Add separate configurable-interval and daily summary cursors.
6. Schedule both inside the existing `watch` process.
7. Migrate S3 and Notion following `migration.md`.
8. Add lifecycle-aware Notion clip behavior.
9. Enable S3 Lifecycle rules.
10. Observe production for several days.
11. Only then consider local recording deletion.

## Acceptance criteria

- A sound-only test produces no new audio Review item and no sound-retained motion clip.
- A real unknown face can still reach the unknown-event pipeline.
- A person detection with no verified face is excluded when the future face-only mode is enabled.
- Every eligible event appears in exactly one interval delta window.
- Changing the interval from two to five hours requires configuration only and does not lose or duplicate events.
- A failed Slack post is retried without losing the window.
- The daily report covers a complete local calendar day.
- One launchd-managed `watch` process reliably handles both interval and EOD schedules.
- A sleeping/restarted Mac catches up without duplicate posts.
- A zero-event daily report states whether Frigate and camera recordings were healthy.
- Thumbnail behavior matches its documented expiry or Slack-file retention contract.
- S3 archive and deletion behavior matches the approved retention table.
- Expired recordings no longer leave misleading live links in Notion.
- No local source file is deleted until its production S3 object is verified and the safety window has passed.
