# fregata-reconciler

Event-driven ingestion service. Reconciles Fregata events and recording segments directly to S3.

## Architecture

Everything that touches footage runs on the Mac Mini. Only derived artifacts — clips and recording segments — leave the house.

```mermaid
flowchart LR
    subgraph door["At the door"]
        CAM["Tapo C260<br/>camera"]
        LOCK["Aqara U400<br/>lock"]
    end

    subgraph mac["Mac Mini — everything local"]
        FREG["Fregata NVR<br/>person + face recognition"]
        LOG["fregata-reconciler<br/>Python · SQLite"]
    end

    subgraph ext["External"]
        S3[("S3 / R2<br/>clips")]
    end

    CAM -->|RTSP| FREG
    FREG -->|"scanned by reconciler"| LOG
    LOG --> S3

    classDef localNode fill:#1e293b,stroke:#64748b,color:#e2e8f0
    classDef extNode fill:#0f172a,stroke:#94a3b8,color:#cbd5e1
    class CAM,LOCK,FREG,LOG localNode
    class S3 extNode
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in credentials
```

Requires Python 3.12+.

### Autostart

```bash
cp launchd/com.swarm.entry-logger.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.swarm.entry-logger.plist
```

Edit the paths in the `.plist` file first. LaunchAgents need a logged-in session, so **enable auto-login on the Mac Mini** or nothing restarts after a power cut.

## Usage

The reconciler takes one of the following commands:

- `inspect`: Inspects the source Fregata database and outputs table/column information.
- `once`: Runs a single reconciliation pass over the Fregata database.
- `watch`: Runs `once` in an infinite loop, sleeping for `POLL_SECONDS` between passes.
- `status`: Outputs the current delivery status, including completed/failed events and uploaded segments.
- `slack-summary`: Posts the Slack unknown-visitor summary immediately, ignoring the schedule (with `DRY_RUN=true` it prints the message instead of posting).

Example:

```bash
python3 reconciler.py watch
```

## Clip links in Notion (optional)

With `CLIP_LINKS=true`, every Notion page gets a working video link in its **Clip**
property (create it in the database first, type **URL** — `python3 test-notion.py`
verifies it). The link opens a viewer page hosted in the S3 bucket, one player per
recording segment. No server runs anywhere: the reconciler presigns the page and its
videos, and the same poll loop that uploads footage re-signs any link older than
`CLIP_REFRESH_SECONDS` (signatures die at `CLIP_URL_TTL_SECONDS`; 7 days is the
SigV4 maximum). Pages synced before the feature existed pick their links up
automatically on the next pass.

Know what you are enabling:

- **The link is a bearer token.** Anyone who can see the Notion page — including via
  a share link or a forwarded URL — can watch that event until the signature expires.
  Keep the database's publish-to-web off. Notion's page history also retains
  superseded links until they expire on their own. And because each refresh rewrites
  the viewer page in place with fresh video URLs, someone who captured a page link
  and re-fetches it near its expiry can reach the videos for up to
  `CLIP_URL_TTL_SECONDS + CLIP_REFRESH_SECONDS` after the capture (~12 days on
  defaults) — shrink both settings if that bound matters to you.
- **Re-signing does not revoke.** Old URLs stay valid to their own expiry. The kill
  switch is deactivating the signing key — set `CLIP_AWS_ACCESS_KEY_ID`/`_SECRET` to
  a dedicated read-only IAM user so that gesture doesn't stop uploads too.
- **Long-term IAM user keys only.** Session credentials (SSO/STS) silently cap the
  signature's life at the session's, and the reconciler can only warn about it.
- **`AWS_REGION` must match the bucket's region.** Uploads survive a mismatch
  (botocore silently redirects) but presigned URLs do not — they fail only at click
  time. The reconciler click-checks one freshly signed link per pass and surfaces a
  failure as a `CLIP FAILED` line in `status`.
- The dedicated signer needs only `GetObject` on the prefix — nothing else. The
  viewer page itself is uploaded by the main credentials, whose policy already has
  `PutObject`.
- If an S3 lifecycle rule expires old segments, the pages for those events keep
  rendering but their videos 404. The link makes existing retention visible.

After fixing a broken setup (Clip property was missing, or the signing key was
rotated), run `python3 reconciler.py clips-reset` to re-sign everything.

## Slack end-of-day summary (optional)

With `SLACK_WEBHOOK_URL` set, the watch loop posts **one message per day** at
`SLACK_SUMMARY_TIME` (local, default 21:00) listing unnamed person events since
the previous summary. There are no per-event pings — the door camera should not
own your notifications — and quiet days post nothing unless
`SLACK_SUMMARY_ON_EMPTY=true`.

Design choices worth knowing:
- **Slack Block Kit Cards.** Summaries are formatted as Slack Block Kit cards
  with table-like event rows: snapshot thumbnail when enabled, time, duration,
  camera/status, and a direct Notion page link.
- **Snapshots are opt-in.** Set `SLACK_INCLUDE_SNAPSHOTS=true` to upload event
  snapshots from Frigate's `media/clips` directory to S3 and include presigned
  image URLs in Slack. Leave it off if you do not want face images retained by
  Slack.
- **Recognized visitors opt-in.** Familiar (household) people can be included
  in the summary by setting `SLACK_INCLUDE_KNOWN=true`. Recognized names and
  visit counts are listed cleanly without attaching video links.
- **Back-facing exits can be filtered.** Set
  `SLACK_UNKNOWN_REQUIRES_FACE=true` to include an unnamed person only when
  Frigate's saved event metadata contains a detected `face` attribute. This
  suppresses typical back-of-head exits but can omit a real unknown entrant
  whose face was never visible. It changes Slack only; footage still reaches
  S3 and Notion.
- **No presigned clip URLs in Slack.** Video links remain out of Slack; each
  event card links to its Notion page instead, which is access-controlled and
  where the clip already lives.
- **Windows are gap-free, not calendar days.** Each summary covers everything
  recorded since the previous one, so an event at 23:50 lands in the next evening's
  message rather than vanishing. If the Mac is asleep at summary time, the first
  pass after it wakes posts one combined catch-up message.
- **Enabling it does not dump history.** The first pass after configuring the
  webhook only opens the window; the first real summary arrives the next scheduled
  time.
- A failed post (Slack down, webhook revoked) retries on the next poll without
  losing the window. `status` reports the last summary time as
  `slack_last_summary`.

Test the pipe end-to-end with `python3 reconciler.py slack-summary`, which posts
immediately (covering the last 24 h if no summary was ever sent).

To send a summary for a **specific historical date** (without modifying the automated schedule cursor):

```bash
python3 reconciler.py slack-summary 2026-08-19
# or with flag:
python3 reconciler.py slack-summary --date 2026-08-19
```

The scheduled message is unknown-only unless `SLACK_INCLUDE_KNOWN=true`. To send
a separate familiar-people summary for a local calendar day, without changing
the scheduled cursor, use the plain numbered-name list:

```bash
python3 reconciler.py slack-people-summary 2026-08-19
# or omit the date for today's local calendar day:
python3 reconciler.py slack-people-summary
```

## Dependency register

Everything the service needs, declared. Nothing here should live only in someone's shell history.

| Dependency | Where declared | Fails how |
|---|---|---|
| Fregata DB accessible | `FREGATA_DB_PATH` | Poll fails, cursor holds |
| S3 credentials | `.env` | Upload retries with backoff |
| Slack webhook valid | `SLACK_WEBHOOK_URL` | Summary retries next poll; deliveries unaffected |
| Auto-login enabled | macOS setting | **Nothing restarts after power loss** |
