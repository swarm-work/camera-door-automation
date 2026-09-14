# Backlog

Items worth doing that are not yet scheduled. Each entry describes the
problem, why it matters, and any known constraints.

---

## 1. Roll out entry-focused single-event clips

**Problem.** The legacy viewer page renders every raw Frigate recording segment
that overlaps a person event plus padding. Frigate stores short segments, so one
Notion event can show several videos, including boundary footage where the person
is absent. Database inspection confirmed that the current Fregata installation
has no configured audio detector and zero audio Review items; the multiplicity
comes from raw segment selection, not sound events.

**Implemented behind a safe flag.** `CLIP_SOURCE=frigate_api` requests one short
MP4 from Frigate's recording-clip API, centered on `event.start_time`, uploads it
as `events/<camera>/<event_id>/clip.mp4`, and makes Notion prefer that direct
object. The legacy default remains `CLIP_SOURCE=segments`. Historical generation
uses the resumable `event-clips-backfill` command.

**Rollout remaining.**

- Deploy the branch to the Mac mini.
- Generate one representative historical clip with `--apply`.
- Confirm the person entering is visible and tune the three-second pre-roll /
  fifteen-second post-start window if needed.
- Switch `CLIP_SOURCE=frigate_api` only after that proof.
- Backfill dates while Frigate still retains their source recordings.
- Keep legacy HTML for events whose source recordings have expired.

**Updated:** 2026-09-14.


## 2. Mac mini and Frigate downtime / outage monitoring in Slack

**Problem.** If the Mac mini loses power, sleeps unexpectedly, or Frigate NVR
crashes / goes offline, camera logging stops silently during the outage. While
the reconciler catches up on missed events once restored, there is no visibility
into how long the system was down or whether coverage was interrupted.

**Proposed behavior:**

- **Mac mini sleep / offline tracking:** The watch loop runs on `POLL_SECONDS`
  (default 30 s). If the elapsed real time between two consecutive poll cycles
  exceeds a threshold (e.g. `> 2 * POLL_SECONDS + 30s` or after system reboot),
  the reconciler records an outage interval (`outage_start` to `outage_end`) in
  the state database.
- **Frigate DB accessibility tracking:** Track failed attempts to connect to or
  snapshot `FREGATA_DB_PATH` to measure Frigate unavailability separately from
  Mac downtime.
- **Slack delivery:** Alongside or right after the scheduled daily summary (or on
  recovery if downtime exceeded an alert threshold), send a dedicated notice:
  `⚠️ System Outage Notice: Mac mini was offline/sleeping for 2h 15m (13:30–15:45)`.

**Recorded:** 2026-09-01.

---

## 3. Prometheus and Grafana observability

**Problem.** Today the reconciler mostly communicates failures through logs,
`status`, and Slack summaries. That is enough for a home Mac mini deployment,
but it does not teach or expose production-style observability concepts.

**Good-to-have learning goal.** Add a small metrics surface that can be scraped
by Prometheus and visualized in Grafana, without making Prometheus/Grafana a
hard requirement for the current Slack/S3/Notion workflow.

**Potential metrics:**

- Reconciliation pass duration and last successful pass timestamp.
- Events seen, completed, failed, and retried.
- Notion sync success/failure counts and gave-up counts.
- Clip refresh success/failure counts.
- Slack summary posts, failures, and last posted window.
- Snapshot uploads attempted/succeeded/failed.
- Frigate DB snapshot failures and downtime windows once outage tracking exists.

**Why it is intentionally later.** Running a metrics endpoint, Prometheus, and
Grafana is overkill for the current single-machine setup, but it is a useful
bridge into monitoring, alerting, dashboards, service-level indicators, and
capacity planning.

**Recorded:** 2026-09-01.

---

## 4. Kubernetes deployment option

**Problem.** The current service is intentionally Mac mini + launchd oriented
because Fregata runs on that host and the recordings live on local disk. A future
deployment may need to run in a Kubernetes cluster for better restart behavior,
deployment hygiene, secret handling, and infrastructure learning.

**Good-to-have learning goal.** Package the reconciler as a containerized service
with Kubernetes manifests or a Helm chart, while keeping the current launchd path
working.

**Technical considerations:**

- Container image for the Python reconciler and its dependencies.
- Secret management for AWS, Notion, and Slack credentials.
- Persistent storage for `reconciler-state.db` or migration to a server-side DB.
- Access to Frigate/Fregata SQLite data and media files from inside the cluster.
- CronJob vs Deployment for scheduled summaries and reconciliation loops.
- Readiness/liveness probes and metrics integration if item 3 exists.

**Why it is gated.** Fregata is currently local to the Mac mini, so Kubernetes
does not help unless the event DB and media paths are made network-accessible or
the camera/NVR stack also moves.

**Recorded:** 2026-09-01.

---

## 5. Native Frigate migration path for non-Mac hardware

**Problem.** The current deployment depends on Fregata on a Mac mini. If the NVR
moves to a Windows or Linux PC with NVIDIA graphics, the project should be able
to run against native Frigate and preserve camera configuration and face data.

**Good-to-have learning goal.** Build a migration pipeline that inventories,
exports, validates, and restores the pieces needed to move from Fregata-on-Mac to
native Frigate on GPU-backed hardware.

**Potential pipeline pieces:**

- Export and version Frigate/Fregata config files.
- Validate camera, detector, object, zone, retention, and recording settings.
- Export face-recognition assets or document exactly which Fregata data is not
  portable to native Frigate.
- Map media paths from macOS layout to Linux/Windows mount layout.
- Dry-run migration checks against a sample native Frigate database.
- Back up and restore `reconciler-state.db` so Notion/S3 deliveries do not
  duplicate after migration.

**Why it matters.** It turns hardware migration into a rehearsable process rather
than a one-off manual rebuild, and it creates a clean learning track for config
management, data migration, backups, and platform portability.

**Recorded:** 2026-09-01.
