# Agent-owned handoff into the YouTube package

The publishing dashboard records creator choices and permission. Its total-control skill hands those choices back to the owning Agent. Only then does the Agent trigger this YouTube package's `scripts/upstream_dispatch.py`. This entry is the start of all YouTube business preparation and execution; the total-control layer must not reproduce these steps or probe translation/OAuth credentials.

## Interface

`python3 /absolute/youtube-publish/scripts/upstream_dispatch.py --output-root /absolute/agent-work/publish-jobs --browser-config /absolute/youtube-browser.local.json [--python /absolute/localization-runtime/python]`

The package root is resolved from the entry's own file location, not from a caller-supplied `--skill-dir`. The optional `--python` selects the runtime with YouTube and localization dependencies; otherwise the package's `.venv` is used. The outer entry itself uses only Python's standard library. This is a local trusted-Agent command, not a network endpoint.

Stdin must contain the original frozen task as one JSON object. Pass it unchanged, including:

- `platform`: exactly `youtube`.
- `decision`: exactly `draft`, `publish`, or `schedule`.
- `jobId`, `sessionId`, `planId`, `planHash`, and `accountProfile`.
- `source`: `assetRevision`, one `media.kind=video` file, and `metadata.title`, `metadata.body`, optional `metadata.cover`. Every file record has its absolute `path`, byte `size`, and lowercase SHA-256 `sha256`.
- `authorization`: the actual persisted dashboard receipt, with `receiptId`, matching `sessionId`, `planId`, `planHash`, matching `assetRevision`, `sourceHash`, `confirmationSource=dashboard`, `trustedUserEventId`, and `confirmedAt`.
- `accountSettings`: the creator-confirmed YouTube settings. Explicit boolean `made_for_kids`, `contains_synthetic_media`, and `notify_subscribers`, plus `privacy`, are required. `made_for_kids=true` requires `notify_subscribers=false`; reject the conflict before creating an attempt or contacting YouTube. The optional `translation_provider` defaults to `argos`; `local_asr_model` chooses its downloaded local speech model. `api_key_env` and `translation_model` apply only when `translation_provider=openai`. Existing `locale`, `thumbnail_no_text`, `tag_region`, and `category_id` retain their meanings.
- `scheduledAt`, `timezone`, `scheduleUtc`: all null for draft/immediate publish. For a schedule, a timezone-aware future timestamp, valid IANA timezone, and matching UTC instant are mandatory.
- `draftScope=local` and `acceptLocalDraft=true` for a draft; never silently substitute a private upload.
- Preserve other frozen upstream fields rather than rewriting the task into a platform manifest outside this package.

`sourceHash` is the SHA-256 of UTF-8 JSON serialized with sorted keys, no extra separators, Unicode preserved, and non-finite numbers rejected. The entry checks source-record bytes before preparing and again before the final action. It does not infer a replacement video, cover, title, account, action, or time.

## Authorization and execution ownership

The caller must read the real saved dashboard confirmation, validate its plan fingerprint and source revision, acquire the existing Agent-side task/worker lock, and verify that the task is not already running, paused, or completed. Do not build a new receipt in order to unblock execution. A copied locator, user chat text, supplied receipt-shaped object, or matching hash alone does not prove consent; this interface cannot authenticate arbitrary stdin. Keep it available only to that trusted Agent execution path.

After validation this package atomically creates `<output-root>/<jobId>/`. An existing directory, even from a failed preparation, returns `JOB_ALREADY_ATTEMPTED`; it is never cleared or replayed. `attempt.json` records task identifiers and a request digest without copying credentials, while `result.json` stores the result. Old paused tasks are not migrated into new attempts. A retry needs human review of the existing state and a separately authorized recovery decision.

The preparation sequence is declarations, local-model readiness, `youtube_localize.py`, manifest creation, dry-run and source/time recheck. Only the final `publish/schedule` call goes to `youtube_browser.py dispatch`, which controls dedicated Chrome. `draft` remains the local `youtube_manifest.py dispatch`. Explicit `--transport api` retains legacy API upload; never automatically switch transports or retry. Read [browser-control.md](browser-control.md) for the external Chrome/channel config. Missing local models still stop preparation; browser login does not supply a translation service.

The existing implementation remains authoritative for English metadata, related tags, timed speech translation/transcription, subtitle burn-in, localized thumbnails, OAuth, and API upload. This entry does not replace those scripts or move them into the total-control package.

## Result contract

Stdout returns one JSON object with `originSkill="youtube-publish"`, `outcome`, `status`, and `message`, plus available result paths or platform IDs. Platform response details must not be upgraded into a stronger success claim.

- Missing local models: `LOCALIZATION_PREFLIGHT_REQUIRED`, `outcome=needs_user`, explicitly owned by this package. The explicit OpenAI compatibility provider uses `LOCALIZATION_KEY_REQUIRED` when its key is absent.
- Draft: only a reported `draft_saved` with `youtube_contacted=false` is considered successful. It is a local prepared bundle, not the YouTube website's drafts.
- Browser publish/schedule: `SUBMITTED` / `SCHEDULE_SUBMITTED` remain pending and include the video ID; they are not proof of public visibility or verified timing. Legacy API `accepted` also means acceptance only.
- Accepted video with failed thumbnail/caption steps: `partial_success`, not a clean success.
- Timeout, malformed final response, or an existing job: outcome remains unknown until reviewed. Never retry automatically.

The owning Agent may relay the receipt to the dashboard. The dashboard only displays the result; it never invokes this command itself.
