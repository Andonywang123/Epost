# Examples and boundaries

## Safe local validation

```bash
.venv/bin/python scripts/youtube_manifest.py validate --manifest /absolute/post/youtube.json
.venv/bin/python scripts/youtube_manifest.py dry-run --manifest /absolute/post/youtube.json
```

Expected: local JSON events only; no OAuth lookup and no network request.

## Explicitly authorized private test

First localize the Chinese master:

```bash
.venv/bin/python scripts/youtube_localize.py localize --video /absolute/post/video.mp4 --source-title "中文标题" --source-description "中文简介" --output-dir /absolute/post/youtube-en
```

Put the emitted READY manifest path in `localization_manifest`. The publishing manifest says `privacy=private`, `notify_subscribers=false`, and contains the creator-confirmed audience declaration:

```bash
.venv/bin/python scripts/youtube_manifest.py publish --manifest /absolute/post/youtube.json --commit
```

This is a real YouTube upload even though it is private.

## Save a prepared draft locally

The manifest must contain `"mode": "draft"`:

```bash
.venv/bin/python scripts/youtube_manifest.py draft --manifest /absolute/post/youtube.json --commit
```

Expected: `draft_saved` with `youtube_contacted=false`. This writes a durable local receipt and does not create a YouTube video.

## Execute an upstream three-route decision

The trigger supplies one literal decision and the manifest carries the same mode:

```bash
.venv/bin/python scripts/youtube_manifest.py dispatch --decision draft --manifest /absolute/post/youtube.json --commit
.venv/bin/python scripts/youtube_manifest.py dispatch --decision publish --manifest /absolute/post/youtube.json --commit
.venv/bin/python scripts/youtube_manifest.py dispatch --decision schedule --manifest /absolute/post/youtube.json --commit
```

A missing or mismatched decision fails before any OAuth or YouTube request. No route falls through to another.

For `schedule`, the manifest must use `mode=schedule`, `privacy=private`, and a timezone-aware future `publish_at`. The dedicated command is:

```bash
.venv/bin/python scripts/youtube_manifest.py schedule --manifest /absolute/post/youtube.json --commit
```

This immediately performs a private upload and asks YouTube to release it at `publish_at`; it does not wait locally until that time.

## Automatic discovery tags

Keep `auto_tags=true` and set `tag_region` when a regional audience matters. Localization creates English semantic tags first. The publish channel may add only related candidates found in recent high-view videos; a search failure falls back to semantic tags without broadening the topic.

## Third-party LLM bridge

```bash
printf '%s' '{"command":"validate","decision":null,"manifest":"/absolute/post/youtube.json","profile":null,"client_secrets":null,"commit":false}' | .venv/bin/python scripts/youtube_tool_bridge.py
```

The host application, not the model, decides whether an exact creator authorization permits `commit=true`.

## Anti-examples

- “素材好了” does not authorize an upload.
- A browser already signed into YouTube does not replace OAuth consent.
- `privacy=private` does not make an upload a dry run.
- `decision=draft` must not be converted into a private upload.
- `decision=schedule` must not be converted into immediate publication, and a publish decision must not inherit a stored schedule.
- A popular but unrelated tag must not be added merely because it has high views.
- Do not reuse a client secret configured as a Web application for an unrelated site's callback.
- Do not change a private manifest to unlisted/public after authorization without asking again.
- Do not upload the Chinese master when localization is incomplete or `NEEDS_REVIEW`.
- Skip English localization only when the creator explicitly says the package is already the final English version.
