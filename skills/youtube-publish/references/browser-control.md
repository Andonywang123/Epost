# Chrome control layer

The default upload transport is `youtube_browser.py` → `youtube_browser.mjs` → Playwright CDP → dedicated Chrome → visible YouTube Studio DOM. The Agent starts one script, not individual screenshot/coordinate actions. No YouTube Data API, private upload endpoints, exported cookies, or Google OAuth client is used by this transport. The default localization and subtitle workflow is local and does not need a translation-service credential; OpenAI is an explicit compatibility provider only.

## Local connection

Install Node dependencies with `npm ci --ignore-scripts`. On macOS start `zsh scripts/start_chrome_cdp.sh /absolute/dedicated-profile 9223`. On other operating systems launch Chrome with the equivalent loopback remote-debugging and dedicated user-data-directory flags. Never use a normal Chrome user profile. The user completes login/security prompts manually.

Run `python3 scripts/youtube_browser.py inspect --cdp-url http://127.0.0.1:9223` to read the current channel without uploading. Store the intended channel in an external local file, not in the portable ZIP:

```json
{"profiles":{"main":{"cdp_url":"http://127.0.0.1:9223","channel_id":"<actual user-confirmed UC channel ID>"}}}
```

The upstream command adds `--browser-config /absolute/youtube-browser.local.json`. Alternatively explicitly provide `--cdp-url` and `--channel-id`, or use the equivalent `EPOST_YOUTUBE_CDP_URL` / `EPOST_YOUTUBE_CHANNEL_ID` environment variables. A connected but wrong channel blocks upload. Do not silently switch accounts. `inspect --inspect-editor` may open the empty upload dialog, but never selects a file.

## Scope of the change

`upstream_dispatch.py` preserves its preparation calls and local draft route. Only the final `publish/schedule` command is switched to the browser bridge. The bridge invokes the same original manifest/localization-resolution functions to obtain the effective English video, metadata, thumbnail and caption. It does not translate or render again. `youtube_localize.py`, `youtube_manifest.py`, and `youtube_publish.py` remain unchanged. `--transport api` is a deliberate compatibility choice, not an automatic retry fallback.

The original `draft` means a local bundle and must remain local. [YouTube's upload help](https://support.google.com/youtube/answer/57407?hl=en) distinguishes uploading from publishing: selecting a file transfers it, and closing an incomplete upload can leave a private video. Therefore neither uploading privately nor closing the editor is treated as a platform draft.

## Safety and results

The real channel and empty upload menu/file input were observed on 2026-09-04. No real upload was made for this change. Post-upload form controls are semantic, fail-closed adapters and still need validation during an authorized release; do not claim full live publishing coverage from the connection check.

Every upload needs a matching decision, manifest and explicit commit, with upstream permission verified by the Agent. Before file selection, a durable browser-attempt record is written. Existing attempts are never replayed. Missing fields, conflicting declarations, identity challenges, unexpected page changes, or expired schedules stop execution. Errors after upload starts are partial/uncertain, not permission to upload again. Keep the Chrome tab and record for review.

The script applies exact creator declarations and metadata, keeps the original English tags, uploads requested cover and captions, and selects visibility. Scheduling must explicitly use a verified Studio UTC timezone; it never assumes the browser timezone equals Studio's selected zone. Unsupported date/control formats stop without submitting, rather than guessing or switching to immediate publish.

Final submission is clicked once. For an immediate public release, the visible Studio share dialog with its publication timestamp is the submission acknowledgement and yields `SUBMITTED`; scheduling uses Studio's scheduled acknowledgement and yields `SCHEDULE_SUBMITTED`. A missing acknowledgement is unknown. A private intermediate upload is not public success. Verification opens the saved video ID, never searches only by title or starts another upload.

CDP attaches to the user-started Chrome without closing it when the script exits. See [Playwright CDP documentation](https://playwright.dev/docs/api/class-browsertype#browser-type-connect-over-cdp). Speed gains come from one attached session, direct file input and semantic locators, not bypassing platform processing or security checks.
