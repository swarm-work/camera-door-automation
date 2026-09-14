# Architecture

How data moves through `reconciler.py` and what is persisted where. Rules for
*changing* this pipeline (privacy, ordering, fail-safe defaults) live in
[`AGENTS.md`](AGENTS.md) — this file only maps the mechanism. Configuration is
documented variable-by-variable in [`.env.example`](.env.example).

## Data flow

`watch` runs `run_once` every `POLL_SECONDS`; each pass snapshots the NVR
database read-only, delivers finalized person events, mirrors them to Notion,
then refreshes stale presigned links. `DRY_RUN=true` remains the safe default.

Event media has two explicit modes:

- `CLIP_SOURCE=segments` uploads every overlapping raw recording segment and
  builds the legacy multi-video HTML page.
- `CLIP_SOURCE=frigate_api` streams one short MP4 centered on the exact
  Frigate `event.id` start, uploads it, and links that object directly.

The independent `face_gallery.py` CLI exports approved face-library images; it
does not participate in the live reconciliation loop.

```mermaid
flowchart TD
    subgraph mac["Mac Mini — local"]
        ENV[".env / Settings"]
        CLI["reconciler.py CLI<br/>once · watch · status · backfill"]
        FACECLI["face_gallery.py<br/>export · verify · restore"]
        FDB[("Fregata SQLite")]
        SNAP[("read-only DB snapshot")]
        REC["raw recording segments"]
        API["local Frigate recording-clip API"]
        FACES["approved clips/faces library"]
        LOOP["run_once / process_event"]
        REFRESH["refresh_clip_links"]
        STATE[("reconciler state DB")]
    end

    subgraph cloud["Private cloud destinations"]
        SEG[("recordings/ raw segments")]
        EVENTCLIP[("events/{camera}/{event_id}/clip.mp4")]
        PAGE[("events/{camera}/{event_id}/index.html")]
        MAN[("optional manifest.json")]
        GALLERY[("content-addressed face-gallery export")]
        NOTION["Notion event database"]
    end

    ENV --> LOOP
    CLI --> LOOP
    FDB --> SNAP --> LOOP
    REC -->|"segments mode"| LOOP
    API -->|"frigate_api mode: streamed entry window"| LOOP
    LOOP <-->|"event, segment, generated-clip state"| STATE
    LOOP -->|"legacy"| SEG
    LOOP -->|"one MP4"| EVENTCLIP
    LOOP --> MAN
    LOOP --> NOTION
    LOOP --> REFRESH
    REFRESH <-->|"link cursor / attempts"| STATE
    REFRESH -->|"legacy viewer"| PAGE
    REFRESH -->|"prefer direct generated MP4"| EVENTCLIP
    REFRESH -->|"PATCH Clip URL"| NOTION
    FACES --> FACECLI --> GALLERY
```

- **Identity association is event-based.** `person` and `face_detected` are
  copied from the same Frigate event and keyed by its full `event.id`.
  Timestamps select reporting or clip windows; they do not join a separate face
  to the nearest event.
- **Privacy defaults remain outbound-safe.** The local state DB stores the
  recognized `sub_label` so summaries can classify events, but a name reaches
  Notion only with `NOTION_INCLUDE_PERSON=true` and reaches a manifest only with
  `UPLOAD_EVENT_MANIFEST=true`. Both default off.
- **Presigned URLs are bearer tokens.** They are SigV4 GETs capped at seven
  days, and temporary SSO/STS credentials shorten their real life. Generated
  clips are linked directly; legacy mode signs an HTML page plus its segments.
- **Approved face exports are isolated.** S3 keys are content-addressed and do
  not include names. Names remain inside the private encrypted-at-rest export;
  `train`, symlinks, hidden content, and unsupported files are excluded.
- **Slack summaries are current functionality.** Unknown summaries can require
  the same event's face attribute with `SLACK_UNKNOWN_REQUIRES_FACE=true`;
  this filter affects Slack, not event delivery.

## Entities

The Fregata tables are read-only and *discovered*, not assumed:
`resolve_table` accepts `event`/`events` and `recordings`/`recording`, checking
each candidate has the required columns (marked `req` below; the other listed
columns are selected only when present). `open_state` creates and migrates the
local delivery schema. Notion properties must already exist in the target
database (`test-notion.py` verifies them).

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
        REAL completed_at "set when selected event media uploaded"
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

    event_clip_delivery {
        TEXT event_id PK "composite destination-aware key"
        TEXT bucket PK
        TEXT endpoint_url PK
        TEXT region
        TEXT source "frigate_api"
        TEXT camera
        REAL start_time "entry window start"
        REAL end_time "entry window end"
        TEXT s3_key
        TEXT etag
        INTEGER size_bytes
        REAL generated_at
        REAL uploaded_at
        TEXT last_error
        REAL updated_at
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
        url Clip "direct generated MP4 or legacy viewer-page URL"
    }

    fregata_event }o--o{ fregata_recording : "legacy camera + padded time-window overlap"
    fregata_event ||--o| event_delivery : "full id copied to event_id"
    fregata_recording ||--o{ segment_delivery : "legacy source_path"
    event_delivery ||--o{ segment_delivery : "legacy media"
    event_delivery ||--o{ event_clip_delivery : "destination-aware generated media"
    event_delivery ||--o| notion_delivery : "event_id"
    notion_delivery |o--o| notion_page : "page_id"
```

- **No foreign keys anywhere.** Legacy raw segments are selected by camera and
  padded overlap. Generated clips use the exact event ID plus a short
  start-anchored API window. State tables share `event_id` by convention.
- **The state DB outlives the source.** Refresh uses the saved camera, generated
  clip or segment keys, and Notion page ID. Historical generation still depends
  on Frigate retaining the underlying recording range.
