# Xiaohongshu image-post manifest

```json
{
  "job_id": "epost-unique-id",
  "account_profile": "creator-id",
  "mode": "draft",
  "title": "胶片机里的温柔浪漫",
  "body": "正文",
  "images": ["/absolute/1.jpg", "/absolute/2.jpg"],
  "tags": ["旅游", "胶片相机", "英国"],
  "auto_tags": true,
  "tag_limit": 8,
  "hot_tag": "travel",
  "visibility": "仅互关好友可见",
  "activity": null,
  "activity_mode": "auto",
  "schedule_at": null
}
```

## Fields

- `job_id`: required stable idempotency key. Reuse it for retries of the same logical post.
- `account_profile`: required Epost account identifier.
- `mode`: `draft`, `publish`, or `schedule`.
- `images`: 1–18 absolute local paths in publication order.
- `tags`: optional creator/model seed topics without the leading `#`. The runtime de-duplicates them.
- `auto_tags`: when true (default), derive more candidates from title/body, then prefer matching platform topic entities and visible popularity signals.
- `tag_limit`: final topic cap from 1 to 10. Activity-required tags are placed first.
- `hot_tag`: optional Epost suggestion. Add at most one and expose it in preview.
- `visibility`: one of the options returned by the current platform UI. The validator recognizes the options observed during testing.
- `activity`: exact activity name only when `activity_mode=exact`; otherwise null.
- `activity_mode`: `auto` chooses only a sufficiently relevant visible activity, `exact` requires the named activity, and `none` disables activity selection.
- `schedule_at`: timezone-aware ISO 8601 time, required when mode is `schedule`; it must satisfy the configured future lead time. The editor converts this instant into the browser timezone and requires an exact UI readback before submission is possible.

## Current adapter coverage

- `visibility: "公开"` is executable with the calibrated adapter. Other visibility values remain valid domain values, but execution stops with `ADAPTER_OUTDATED` until their current controls are calibrated.
- Activity selection is best-effort for `auto`: a missing widget or no relevant candidate is reported and skipped. `exact` never silently substitutes another activity.
- `hot_tag`, when present and not duplicated in `tags`, is inserted as one additional topic attempt.
- Validation reads local files and emits a deterministic `content_fingerprint`; it performs no browser action.
