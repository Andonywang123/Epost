---
name: youtube-publish
description: Prepare an English YouTube release with local Chinese-to-English translation, subtitles and cover localization, then use a deterministic script connected to dedicated Chrome and YouTube Studio to publish or schedule. Preserve local drafts and exact upstream decisions. Use for YouTube 一键发布、Chrome 网页脚本发布、英文字幕、草稿或定时分发。No upload without exact authorization; API transport is an explicit legacy option only.
---

# YouTube English Publish

Use the packaged scripts as the execution layer. A Chinese master is the source of truth. By default, Argos Translate, faster-whisper and RapidOCR prepare English metadata, subtitles and a cover locally; this route does not require an OpenAI API key or send creator media to a translation service. OpenAI remains an explicit compatibility provider. Final upload control uses a Node/Playwright script connected over CDP to dedicated Chrome and YouTube Studio. Do not use Codex screenshot-by-screenshot browser control as the normal path. No YouTube Data API/OAuth grant is required for the browser route.

## Agent-triggered tasks from the publishing dashboard

When the owning Agent has verified a persisted dashboard confirmation, use this package's single [upstream dispatch entry](references/upstream-dispatch.md). The total-control skill only gathers the creator's requirements and hands the frozen task to the Agent; it does not prepare YouTube files, check localization readiness, translate, render subtitles, or call the lower-level publishing commands.

```bash
python3 scripts/upstream_dispatch.py --output-root /absolute/agent-work/publish-jobs --python /absolute/youtube-runtime/bin/python
```

Send the original complete confirmed JSON task on stdin. Add `--browser-config /absolute/youtube-browser.local.json` to the trusted command. This entry preserves the preparation and local-draft sequence; `publish/schedule` use `youtube_browser.py` for the final action by default. Missing local language or speech models remain a `youtube-publish` result, not a total-control preflight. Do not run another manual workflow after this entry or retry an existing job. The Agent must first check saved approval/history; matching JSON fields do not create authorization. Read [Chrome control](references/browser-control.md) for connection and result boundaries.

## Runtime

For the default browser route, install this package's Node dependency with `npm ci --ignore-scripts`. Start a dedicated Chrome using `zsh scripts/start_chrome_cdp.sh /absolute/dedicated-profile 9223`, let the user log in, then run `python3 scripts/youtube_browser.py inspect --cdp-url http://127.0.0.1:9223`. Bind the returned channel ID to the intended profile in an external browser config. Never use the ordinary Chrome profile, copy cookies, or expose CDP off-device.

Create a virtual environment and install the publishing and localization dependencies once:

```bash
python3 -m venv .venv
.venv/bin/pip install -r scripts/requirements-localization.txt
.venv/bin/python scripts/youtube_localize.py install-local-models
```

`install-local-models` downloads the Chinese→English Argos language package and the default local speech model once. It does not use an OpenAI key. The normal dashboard path selects `translation_provider=argos` by default; set `translation_provider=openai` only when the creator has separately configured an OpenAI API key and wants the compatibility route.

Install a system `ffmpeg` build that includes the `subtitles` filter backed by libass. The bundled `imageio-ffmpeg` fallback may not include libass, so `youtube_localize.py preflight` is the authority.

YouTube browser login and API authorization are separate. Do not ask for OAuth/client JSON on the default browser route. Only when the user explicitly chooses legacy `--transport api`, use the original Desktop-app OAuth setup below. Never store passwords, browser cookies, or client JSON inside this skill.

## Default final action: Chrome script

Keep the original localization and manifest preparation. For an already prepared manifest, run `python3 scripts/youtube_browser.py dispatch --decision publish --manifest /absolute/youtube.json --browser-config /absolute/youtube-browser.local.json --commit` (or `schedule` matching manifest mode). This bridge reuses the original preparation functions; it does not regenerate media or change the selected action. The script checks the exact channel, uploads through the observed file input, applies metadata/thumbnail/captions and visibility, and submits once. Use `dry-run --manifest ...` for local mapping only, or `inspect` for a read-only connection check.

`draft` retains the original local-only behavior via `youtube_manifest.py`; browser upload-and-close is not a platform draft. Do not change upstream draft scope in order to use Chrome. Errors after file selection may already have created a private upload: keep the tab and attempt receipt, never select the video again automatically. `SUBMITTED` and `SCHEDULE_SUBMITTED` remain pending, not proof of public visibility or verified timing. Current real-page calibration covers login/channel and the empty upload entry; later upload fields have not been tested with a real submission in this change.

## Original API workflow (explicit compatibility route only)

The original preparation steps below remain authoritative. Their API upload commands are only for explicit `--transport api`; default final publishing uses the Chrome script above. Do not run both transports for one job.

1. Read [references/payload-schema.md](references/payload-schema.md) and create one manifest beside the local post assets. Set `mode=draft|publish|schedule` from the upstream decision and require the creator to declare visibility, made-for-kids, and synthetic-media values before a real upload.
2. Run manifest `validate`. It is local-only and does not contact Google.
3. Read [references/localization.md](references/localization.md), run localization `preflight`, then run `youtube_localize.py localize`. Translate the Chinese title and description, generate 6-15 relevant English discovery tags, translate or transcribe timed speech, produce an English WebVTT track, burn readable English subtitles into a new video, and localize a supplied Chinese thumbnail.
4. Add the resulting `status=READY` localization manifest path to `localization_manifest`, then run manifest `dry-run`. Do not upload the Chinese source, Chinese caption, or Chinese thumbnail after localization failure. `NEEDS_REVIEW` blocks dispatch.
5. Match the three-route decision exactly:
   - `draft`: save a durable local draft receipt and prepared English assets; do not contact YouTube. The YouTube Data API has no platform draft resource, so never label a private upload as a draft.
   - `publish`: before the upload, optionally refine the generated tags with tags found on related recent high-view videos in the configured region; then perform the requested upload.
   - `schedule`: require `privacy=private` and a timezone-aware future `publish_at`, then upload once with YouTube's scheduled release time.
   - `dispatch --decision draft|publish|schedule`: require the decision to equal manifest `mode`; reject missing or mismatched values before any upload.
6. Before the first real publish, follow [references/oauth-setup.md](references/oauth-setup.md), run `auth`, complete consent in the browser, then run YouTube `preflight`.
7. Match lower-level actions exactly:
   - inspection only: `validate`, `dry-run`, or `preflight`;
   - local draft: manifest mode `draft` plus `draft --commit`;
   - publish now: manifest mode `publish` plus `publish --commit`;
   - scheduled release: manifest mode `schedule`, `privacy=private`, a future `publish_at`, plus `schedule --commit`;
   - legacy compatibility alias: `upload --commit` accepts only publish or schedule manifests.
8. Return the emitted status without upgrading its meaning. An accepted private upload is not a public publication. API acceptance is not completed YouTube processing.
9. Reuse the same `job_id` only for retries of the same logical video and exact metadata/visibility. The uploader rejects a reused job ID when the video, tags, metadata, or visibility changed.

## Original API commands (compatibility)

```bash
.venv/bin/python scripts/youtube_manifest.py validate --manifest /absolute/post/youtube.json
.venv/bin/python scripts/youtube_manifest.py dry-run --manifest /absolute/post/youtube.json
.venv/bin/python scripts/youtube_localize.py preflight
.venv/bin/python scripts/youtube_localize.py localize --video /absolute/post/video.mp4 --source-title "中文标题" --source-description "中文简介" --source-caption /absolute/post/中文.srt --thumbnail /absolute/post/中文封面.jpg --output-dir /absolute/post/youtube-en
.venv/bin/python scripts/youtube_publish.py auth --client-secrets /absolute/client_secret.json --profile main
.venv/bin/python scripts/youtube_publish.py preflight --profile main
.venv/bin/python scripts/youtube_manifest.py draft --manifest /absolute/post/youtube.json --commit
.venv/bin/python scripts/youtube_manifest.py publish --manifest /absolute/post/youtube.json --commit
.venv/bin/python scripts/youtube_manifest.py schedule --manifest /absolute/post/youtube.json --commit
.venv/bin/python scripts/youtube_manifest.py dispatch --decision draft --manifest /absolute/post/youtube.json --commit
.venv/bin/python scripts/youtube_manifest.py dispatch --decision publish --manifest /absolute/post/youtube.json --commit
.venv/bin/python scripts/youtube_manifest.py dispatch --decision schedule --manifest /absolute/post/youtube.json --commit
```

Omit `--source-caption` to transcribe Chinese speech automatically. Omit `--thumbnail` when no thumbnail was supplied. The agent may execute this sequence from one user request, but only publish/schedule routes with `--commit` perform the external upload, and each still requires authorization for the exact manifest, visibility, and schedule.

`publish --commit`, `schedule --commit`, and their matching dispatch decisions create a YouTube video resource and transfer the video. `draft --commit` writes only a local receipt and never contacts YouTube. Never treat `privacy=private` as a draft or dry run.

## Core rules

- Keep the local manifest and media as the source of truth. Credentials and job state stay outside the skill directory.
- For this skill, Chinese input is localized by default. Skip localization only when the creator explicitly says the supplied package is already the final English version.
- Only a localization manifest with `status=READY` is publishable. Low-confidence transcription, missing/reordered cues, remaining unapproved Chinese, invalid timestamps, failed subtitle burn-in, or failed thumbnail localization produces `NEEDS_REVIEW` and blocks upload.
- Always keep both outputs: an English WebVTT accessibility track and a video copy with burned-in English subtitles. Upload the WebVTT after YouTube accepts the video.
- Treat `draft`, `publish`, and `schedule` as literal upstream decisions. Never substitute one route for another, infer a missing decision, or upload when decision and manifest mode differ.
- Generate relevant English tags during localization. The browser route retains those tags without requiring an API lookup; do not claim live popularity was verified. Explicit API transport retains its existing read-only refinement and semantic fallback. Never add an unrelated popular tag.
- Default a technical upload test to `privacy=private`, `notify_subscribers=false`, and a unique job ID, but disclose that assumption before uploading. This is still a publish-channel upload, not a draft.
- Require the creator to explicitly declare `made_for_kids`. Do not infer audience classification from a filename.
- Reject `made_for_kids=true` with `notify_subscribers=true` before creating an attempt or contacting YouTube; return to the dashboard for a new confirmed choice instead of silently disabling notifications.
- Preserve `contains_synthetic_media=null` when unknown; require a creator decision before a public release when disclosure may apply.
- A scheduled video must be private and `publish_at` must include a timezone and be in the future.
- Never reuse an unrelated Web OAuth client. Local authorization requires a Desktop app client and a loopback redirect.
- Stop automatic retries for OAuth, invalid metadata, quota, policy, copyright, and permission errors. Retry only network and documented transient status failures.
- Do not claim public automation is production-ready until the Google Cloud project passes any required YouTube API compliance audit. New unaudited projects may have API uploads locked to private.
- Treat thumbnail or caption failure after video acceptance as partial success. Report the video ID and each failed substep.

Use [references/localization.md](references/localization.md) for localization details. Use [references/rules.md](references/rules.md) for authorization, privacy, idempotency, status, and retries. Use [references/examples.md](references/examples.md) for valid calls and boundary cases. Read [references/api-notes.md](references/api-notes.md) only when changing API fields or diagnosing platform limitations.

For third-party LLM function calling, the original `assets/tool-schema.json` and `scripts/youtube_tool_bridge.py` are API-compatibility interfaces only. Default frozen Agent tasks use `upstream_dispatch.py` and its browser config. Keep exact action authorization outside model-generated parameters.
