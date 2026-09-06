# YouTube publishing rules

These original API/draft rules remain for explicit API compatibility. The default upload control is now the [Chrome script](browser-control.md); its exact authorization, no-repeat and local-draft semantics are the same. Browser login replaces upload OAuth only, not translation-service configuration.

## Authorization

- `validate`, `dry-run`, and `preflight` do not create or change a YouTube video.
- `auth` creates/replaces a local OAuth grant and requires an intentional account-setup request.
- `draft --commit` writes a durable local receipt and prepared bundle only. It never contacts YouTube.
- `publish --commit`, `schedule --commit`, matching dispatch decisions, and legacy `upload --commit` can create a YouTube video. Authorization is scoped to the exact manifest, account profile, privacy, and schedule shown to the creator.
- `dispatch --decision draft|publish|schedule --commit` is the stable three-route router. The decision must equal manifest `mode`; a missing or mismatched value is rejected before OAuth or network access.
- Changing the video, account, privacy, schedule, audience declaration, or synthetic-media declaration invalidates earlier upload authorization.

## Status meanings

- `validated`: local structure and files passed checks; Google was not contacted.
- `dry_run_ok`: the YouTube request body was constructed locally; Google was not contacted.
- `draft_saved`: a local prepared-bundle receipt was written; YouTube was not contacted and no YouTube video ID exists.
- `authorized`: a refreshable OAuth grant was stored in the OS keychain.
- `preflight_ok`: the authorization resolves to a YouTube channel.
- `accepted`: YouTube returned a video ID and requested metadata was sent.
- `partial_success`: YouTube accepted the video, but a later thumbnail or caption step failed.
- `processing_timeout`: the upload exists, but polling ended before processing reached a final state.

Never translate `accepted` into “publicly visible”. Visibility and processing are independent.

## Idempotency and retries

- The job state lives under `${EPOST_STATE_DIR:-~/.local/state/epost}` and contains no OAuth secret.
- The same `job_id`, video hash, and release-request fingerprint reuse the known upload result. Changed metadata, visibility, disclosure, schedule, or tags with the same job ID are rejected.
- Retry rate limits and transient network/5xx failures with exponential backoff.
- Do not blindly retry invalid metadata, authentication/permission, policy, copyright, or hard quota errors.
- After an uncertain response, inspect saved job state before starting another logical upload.

## Privacy and scheduling

- Use `private` for technical tests unless the user explicitly requests a broader visibility.
- `unlisted` and `public` are externally visible outcomes and require explicit authorization.
- Scheduling uses `privacy=private` plus a timezone-aware future `publish_at`.
- A scheduled dispatch is a real private upload whose `status.publishAt` is set by YouTube; it is not a local delayed job or a draft.
- An unaudited API project may have all API uploads forced to private regardless of the requested value.

## Tag matching

- Produce English discovery tags from the localized title, description, and transcript context; reject empty, duplicate, Chinese-containing, and over-limit values.
- When `auto_tags=true`, run one read-only YouTube search immediately before publish for recent, related, high-view videos in `tag_region`, then rank candidate tags by semantic relevance before repetition and view signals.
- Do not use a global popularity chart as proof that a tag fits this video, and never inject an unrelated popular tag.
- Merge activity-independent creator tags, localized semantic tags, and verified live candidates deterministically. If the search fails, keep semantic tags and emit a warning.

## Draft semantics

The YouTube Data API has no platform draft state. A private upload creates a real video resource and belongs to the publish channel. Never reinterpret `decision=draft` as `privacy=private`.
