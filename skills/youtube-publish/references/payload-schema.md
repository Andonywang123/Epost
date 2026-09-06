# YouTube video manifest

```json
{
  "job_id": "youtube-unique-id",
  "account_profile": "main",
  "mode": "draft",
  "video": "/absolute/video.mp4",
  "title": "视频标题",
  "description": "视频简介",
  "tags": ["标签一", "标签二"],
  "auto_tags": true,
  "tag_region": "US",
  "category_id": "22",
  "default_language": "zh-CN",
  "privacy": "private",
  "publish_at": null,
  "made_for_kids": false,
  "contains_synthetic_media": null,
  "notify_subscribers": false,
  "thumbnail": null,
  "caption": null,
  "caption_language": null,
  "caption_name": null,
  "localization_manifest": null,
  "poll_processing": true
}
```

## Fields

- `job_id`: stable local idempotency key. Reuse only while retrying the same logical video.
- `account_profile`: profile name mapped to the expected Chrome channel in the external browser config; on explicit legacy API transport it refers to the Keychain profile created by `auth`. It is not a password.
- `mode`: `draft` for a durable local prepared bundle, `publish` for an immediate release, or `schedule` for a future YouTube release. Default final upload control is Chrome; explicit API transport uses the same manifest. Legacy input `upload` is normalized to `publish`.
- `video`: required existing absolute local path.
- `title`: required, at most 100 characters.
- `description`: at most 5000 characters.
- `tags`: creator-supplied seed tags without `#`. Localized English tags and live discovery candidates are merged without deleting these seeds.
- `auto_tags`: defaults to true. Generate relevant English discovery tags during localization and, immediately before publish, refine them with one read-only search for related recent high-view videos. A lookup failure keeps the semantic fallback tags and does not block the upload.
- `tag_region`: optional two-letter region used by live discovery refinement; default `US`.
- `category_id`: YouTube video category ID; `22` is the template default, not a content inference.
- `default_language`: metadata/video default language such as `zh-CN` or `en-GB`.
- `privacy`: `private`, `unlisted`, or `public`. Even a private value still performs a real upload.
- `publish_at`: timezone-aware future ISO 8601 time; required only for `mode=schedule`. The runtime passes the normalized instant as YouTube `status.publishAt`; scheduled uploads must remain private until release.
- `made_for_kids`: required boolean creator declaration.
- `contains_synthetic_media`: boolean disclosure or `null` while unknown.
- `notify_subscribers`: whether YouTube should notify subscribers; default false for tests and must be false when `made_for_kids=true`.
- `thumbnail`: optional existing absolute path.
- `caption`: optional timed caption file. When supplied, `caption_language` is required.
- `caption_name`: optional track name.
- `localization_manifest`: READY output from `youtube_localize.py`. It is required for the default Chinese-to-English workflow and may be null only when the creator explicitly declares that the supplied package is already the final English version.
- `poll_processing`: whether to wait for YouTube transcoding status after upload.

Validation reads local files and produces a SHA-256 content fingerprint. It does not authorize or contact YouTube. The idempotency fingerprint also includes effective metadata, visibility, disclosure, schedule, and tags so a changed release cannot silently reuse an earlier upload result.

The manifest keeps the Chinese `video`, `title`, `description`, `caption`, and `thumbnail` as source inputs. When `localization_manifest` is present, the uploader replaces those publishing fields with the verified localized video, English metadata, English WebVTT, and localized thumbnail from that READY bundle.

YouTube Data API does not expose a platform draft resource. `mode=draft` therefore writes a local `*.draft.json` receipt containing the validated manifest and prepared English assets; it performs no OAuth lookup and no YouTube request. A private YouTube upload is still `mode=publish`, never a draft.
