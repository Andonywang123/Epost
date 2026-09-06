# YouTube API notes

Read this reference when setting up credentials, changing upload fields, or diagnosing an API limitation.

## Official endpoints

- OAuth 2.0: <https://developers.google.com/youtube/v3/guides/authentication>
- Video upload: <https://developers.google.com/youtube/v3/docs/videos/insert>
- Resumable upload: <https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol>
- Video status and scheduling: <https://developers.google.com/youtube/v3/docs/videos>
- Captions: <https://developers.google.com/youtube/v3/docs/captions/insert>
- Thumbnails: <https://developers.google.com/youtube/v3/docs/thumbnails/set>
- Related discovery search: <https://developers.google.com/youtube/v3/docs/search/list>

## Integration decisions

- Use a server or installed-app OAuth flow with `access_type=offline`. The refresh token permits unattended access-token renewal; YouTube does not support service accounts for channel access.
- `videos.insert` accepts title, description, tags, category, default language, localization, privacy, `publishAt`, made-for-kids, and synthetic-media disclosure fields.
- Scheduled publishing requires `privacyStatus=private`; `publishAt` is valid only for a video that has never been public.
- Use `uploadType=resumable`. Persist the resumable URI and query upload progress before retransmitting bytes after an uncertain failure.
- Poll `videos.list(part=status,processingDetails)` after upload. Distinguish API acceptance, media processing, and public availability.
- A custom thumbnail is a separate `thumbnails.set` call and still depends on the channel's feature eligibility or verification.
- `captions.insert` requires a timestamped caption file. The API no longer auto-synchronizes untimed captions.
- The API exposes `private`, `unlisted`, and `public` privacy statuses but no platform draft resource. This skill's draft channel is therefore local-only; it must never create a private video.
- For live tag refinement, use `search.list` ordered by views over a recent window, then `videos.list` for snippets and statistics. Filter by relevance before frequency and views. Do not use the broad `mostPopular` chart as a topic-match oracle.

## Product blockers

- API projects created after 28 July 2020 are restricted to private uploads until they pass a YouTube API compliance audit. Treat the audit as a launch dependency.
- Current official documentation gives `videos.insert` a separate upload quota and a default limit of 100 calls per day. Caption insertion consumes the general quota and is comparatively expensive. Monitor both quota buckets before dispatch.
- YouTube processing and policy review can exceed Epost's 15-minute SLA. Define success for the SLA as: localized assets ready, bytes uploaded, video ID returned, and privacy or schedule accepted. Continue reporting platform processing afterward.
