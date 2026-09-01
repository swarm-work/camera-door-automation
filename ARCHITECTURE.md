# Architecture

How data moves through `reconciler.py` and what is persisted where. Rules for
*changing* this pipeline (privacy, ordering, fail-safe defaults) live in
[`AGENTS.md`](AGENTS.md) — this file only maps the mechanism. Configuration is
documented variable-by-variable in [`.env.example`](.env.example).

## Data flow

One process, no server. `watch` runs `run_once` every `POLL_SECONDS`; each pass
snapshots the NVR database read-only, uploads any finalized events' footage,
mirrors each event to Notion, and only then refreshes stale presigned clip
links (delivery ordering — see AGENTS.md Invariants). `DRY_RUN=true` (the
default) logs every S3 and Notion write instead of performing it.

```mermaid
flowchart TD
    subgraph mac["Mac Mini — everything local"]
        ENV[".env / environment<br/>Settings.from_env"]
        CLI["CLI entry points<br/>inspect · once · watch · status · clips-reset"]
        FDB[("Fregata NVR SQLite DB<br/>FREGATA_DB_PATH")]
        SNAP[("temp read-only snapshot<br/>snapshot_db, sqlite backup API")]
        REC["recordings dir<br/>FREGATA_RECORDINGS_DIR<br/>segment files"]
        LOOP["run_once delivery pass<br/>resolve_table → fetch_events → process_event"]
        REFRESH["refresh_clip_links<br/>after the delivery loop, same pass"]
        KEYS["signing identity<br/>CLIP_AWS_* read-only key,<br/>else main AWS credentials"]
        STATE[("state DB<br/>STATE_DB_PATH")]
    end

    subgraph cloud["Cloud"]
        subgraph bucket["S3 / R2 bucket"]
            SEG[("recordings/… segments")]
            PAGE[("events/{camera}/{event_id}/index.html<br/>viewer page")]
            MAN[("events/{camera}/{event_id}/manifest.json<br/>optional")]
        end
        NOTION["Notion database<br/>one page per event"]
    end

    subgraph browser["Viewer's browser"]
        V["anyone who can see the Notion page"]
    end

    ENV --> LOOP
    CLI -->|"once / watch"| LOOP
    CLI -->|"inspect"| SNAP
    CLI -->|"status reads · clips-reset clears clip state"| STATE
    FDB -->|"read-only backup, every pass"| SNAP
    SNAP -->|"fetch_events + fetch_segments"| LOOP
    REC -->|"segment files, after SETTLE_SECONDS"| LOOP
    LOOP <-->|"delivery bookkeeping"| STATE
    LOOP -->|"upload_file: PutObject + head_object"| SEG
    LOOP -->|"only if UPLOAD_EVENT_MANIFEST=true —<br/>embeds the raw event row incl. sub_label"| MAN
    LOOP -->|"sync_notion: create page —<br/>Person is sub_label only if NOTION_INCLUDE_PERSON=true,<br/>else Unrecognized"| NOTION
    LOOP --> REFRESH
    KEYS --> REFRESH
    REFRESH <-->|"pick never-linked + stale rows,<br/>record clip_signed_at"| STATE
    REFRESH -->|"put_object: page rewritten with<br/>freshly presigned video URLs"| PAGE
    REFRESH -->|"PATCH Clip = presigned page URL"| NOTION
    REFRESH -.->|"verify_clip_url: one real GET per pass"| PAGE
    NOTION -->|"event page"| V
    V -->|"clicks Clip URL — a bearer token"| PAGE
    PAGE -.->|"video tags load presigned segment URLs"| SEG
```

- **Where a person's name can travel.** `sub_label` (face recognition) leaves
  the snapshot on exactly two edges, both default-off: into the manifest when
  `UPLOAD_EVENT_MANIFEST=true` (the manifest embeds the whole raw event row),
  and into the Notion `Person` property when `NOTION_INCLUDE_PERSON=true`
  (otherwise every page reads "Unrecognized"). It is never written to the state
  DB, the viewer page, or logs.
- **Presigned URLs are bearer tokens.** The Clip link and the video URLs
  inside the viewer page are SigV4 presigned GETs (TTL `CLIP_URL_TTL_SECONDS`,
  clamped to the 7-day SigV4 ceiling). `refresh_clip_links` re-signs anything
  older than `CLIP_REFRESH_SECONDS`; re-signing never revokes, so the kill
  switch is deactivating the dedicated read-only `CLIP_AWS_*` key. The full
  threat model is the "Know what you are enabling" list in `README.md`.
- **Slack daily summary** (end-of-day digest of unrecognized visitors) exists
  only on the unmerged `slack-daily-summary` branch (PR #8) and is therefore
  not drawn; its fixed contract is in AGENTS.md Invariants.

## Entities

Three stores. The Fregata tables are read-only and *discovered*, not assumed:
`resolve_table` accepts `event`/`events` and `recordings`/`recording`, checking
each candidate has the required columns (marked `req` below; the other listed
columns are selected only when present). The state DB schema is created by
`open_state` in `reconciler.py`, which also `ALTER TABLE`s older state DBs up
to the current shape. The Notion properties must already exist in the target
database (`test-notion.py` verifies them; `notion-database-template.csv` is a
template).

```mermaid
erDiagram
    fregata_event {
        TEXT id PK "req"
        TEXT camera "req; filtered by CAMERA when set"
        TEXT label "req; filtered by LABEL, default person"
        REAL start_time "req"
        REAL end_time "req; only finalized events - end_time IS NOT NULL"
        TEXT sub_label "optional; a person's name from face recognition"
        REAL top_score "optional"
        INTEGER false_positive "optional"
        TEXT zones "optional; JSON"
        INTEGER has_clip "optional"
        INTEGER has_snapshot "optional"
        TEXT data "optional; JSON"
    }

    fregata_recording {
        TEXT camera "req"
        TEXT path "req; resolved via canonical_path"
        REAL start_time "req"
        REAL end_time "req"
        TEXT id "optional"
        REAL duration "optional"
        INTEGER objects "optional"
        INTEGER motion "optional"
        INTEGER regions "optional"
        INTEGER segment_size "optional"
    }

    event_delivery {
        TEXT event_id PK "copied from fregata_event.id"
        TEXT camera "NOT NULL"
        REAL start_time "NOT NULL"
        REAL end_time "NOT NULL"
        TEXT manifest_key "NULL unless UPLOAD_EVENT_MANIFEST"
        TEXT person "recognized sub_label; NULL for unnamed person"
        INTEGER face_detected "1 when saved Frigate event metadata contains face attribute"
        REAL recorded_at "first insertion time; Slack summary cursor field"
        REAL completed_at "set when all segments uploaded"
        TEXT last_error
        REAL updated_at "NOT NULL"
    }

    segment_delivery {
        TEXT event_id PK "composite PK with source_path"
        TEXT source_path PK "canonical local path"
        TEXT s3_key "NOT NULL"
        TEXT etag
        REAL uploaded_at
    }

    notion_delivery {
        TEXT event_id PK
        TEXT page_id "Notion page id"
        REAL synced_at "set once the page exists"
        TEXT last_error
        INTEGER attempts "page-creation budget, cap NOTION_MAX_ATTEMPTS"
        REAL clip_signed_at "NULL means never linked - backlog eligible"
        INTEGER clip_attempts "refresh budget; reset to 0 on success"
        REAL updated_at "NOT NULL"
    }

    notion_page {
        title Event_ID "property Event ID; dedupe key for lookups"
        select Person "property Person; sub_label or Unrecognized"
        select Camera "property Camera"
        date Seen "property Seen; local-time start to end"
        number Duration_s "property Duration (s)"
        number Score "property Score; only when top_score present"
        number Segments "property Segments; delivered segment count"
        rich_text Manifest_key "property Manifest key; empty when manifest off"
        url Clip "property Clip; presigned viewer-page URL, PATCHed by refresh"
    }

    fregata_event }o--o{ fregata_recording : "camera match + padded time-window overlap, no FK"
    fregata_event ||--o| event_delivery : "id copied to event_id"
    fregata_recording ||--o{ segment_delivery : "path canonicalized to source_path"
    event_delivery ||--o{ segment_delivery : "event_id, no FK constraint"
    event_delivery ||--o| notion_delivery : "event_id; JOINed by refresh_clip_links"
    notion_delivery |o--o| notion_page : "page_id, NULL until the page exists"
```

- **No foreign keys anywhere.** Events and recordings are related only by
  `fetch_segments`' query: same `camera`, and the recording's time range
  overlapping the event window padded by `PRE_ROLL_SECONDS` /
  `POST_ROLL_SECONDS`. The three state tables share `event_id` by convention;
  the one SQL JOIN is `notion_delivery ⋈ event_delivery` in the refresh pass
  (which also means clip links survive Fregata's retention evicting the
  source event).
- **The state DB outlives the source.** Everything the refresh pass needs —
  camera, segment `s3_key`s, page id — comes from the state DB, never from
  Fregata.
