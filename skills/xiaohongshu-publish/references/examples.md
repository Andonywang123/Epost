# Examples and boundary cases

All paths below must be absolute. Replace profile and manifest paths with local values.

## Safe validation

```bash
node scripts/xhs_publisher.mjs validate \
  --manifest /Users/name/posts/example/.epost/manifests/xiaohongshu.json
```

Expected status: `VALID`. No browser is opened.

## Session preflight

```bash
scripts/start_chrome_cdp.sh /Users/name/.epost/chrome/xiaohongshu
node scripts/xhs_publisher.mjs preflight \
  --cdp-url http://127.0.0.1:9222
```

Expected status: `PREFLIGHT_OK` or `NEEDS_USER`. This does not fill or submit content.

If it returns `LOGIN_REQUIRED`, complete login in the already-open Chrome and run preflight again. CDP mode does not close Chrome.

## Supervised fill

The manifest can use any mode because this command only fills the editor. The explicit flag acknowledges that editor auto-save may occur.

```bash
node scripts/xhs_publisher.mjs fill \
  --manifest /Users/name/posts/example/.epost/manifests/xiaohongshu.json \
  --cdp-url http://127.0.0.1:9222 \
  --commit
```

Expected status: `FILLED`. It is not evidence of a saved draft or submission.

## Save a draft

The manifest must contain `"mode": "draft"`.

```bash
node scripts/xhs_publisher.mjs draft \
  --manifest /Users/name/posts/example/.epost/manifests/xiaohongshu.json \
  --cdp-url http://127.0.0.1:9222 \
  --commit
```

Expected status: `DRAFT_SAVED`. Stop if the current UI has no explicit draft control.

## Execute an upstream decision

The trigger must send one literal decision and the manifest must carry the same mode:

```bash
node scripts/xhs_publisher.mjs dispatch --decision draft --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs dispatch --decision publish --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs dispatch --decision schedule --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
```

No route falls through to another. A missing or mismatched decision returns `VALIDATION_FAILED` before the browser opens.

## Automatic topics and activity

Use `auto_tags=true`, `tag_limit=8`, and `activity_mode=auto`. The adapter derives topic seeds from title/body, prefers matching platform suggestions, selects an activity only above the relevance threshold, and puts the activity's required tags first. `ACTIVITY_SKIPPED_NO_MATCH` is an allowed non-fatal warning.

## Schedule or publish

For `schedule`, the manifest needs a timezone-aware future `schedule_at` and mode `schedule`. For immediate submission, use mode `publish` and the `publish` command.

```bash
node scripts/xhs_publisher.mjs schedule --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs publish --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
```

The first may return `SCHEDULED`; the second may return `SUBMITTED`, `UNDER_REVIEW`, or `PUBLISHED`.

Running `fill --commit` with a schedule manifest is the safe preview path: it enables the scheduling control, converts the instant into the browser timezone, writes the time, and requires an exact readback, but does not click `定时发布`.

## Anti-examples

- Do not add `--commit` because the user only said “素材准备好了”. Validate instead.
- Do not run `publish` for a manifest whose mode is `draft`; the runtime rejects the mismatch.
- Do not send `dispatch --decision publish` for a draft manifest; the runtime rejects it before browser access.
- Do not choose an unrelated high-popularity activity. Relevance is the hard gate.
- Do not turn a rejected scheduled time into immediate publication.
- Do not click publish again after a timeout. Run `verify` and return `UNKNOWN` if evidence is insufficient.
- Do not treat literal `#话题` fallback as an inserted platform topic entity; preserve the warning.
- Do not use the normal Chrome directory such as `~/Library/Application Support/Google/Chrome` as `--profile-dir`.
- Do not expose the CDP endpoint on `0.0.0.0` or a public interface.

## Boundary fixtures

- Title length 20 is accepted; 21 is rejected.
- Body length 1000 is accepted; 1001 is rejected.
- Image count 1–18 is accepted; 0 or 19 is rejected.
- Supported image suffixes are `.jpg`, `.jpeg`, `.png`, and `.webp`; each file must exist and be at most 32 MB.
- `schedule_at` must include a timezone and satisfy the configured lead time, currently 60 minutes in the validator.

## Regression notes

- 2026-09-02: the creator home initially loaded with an empty body and redirected to `/login?...redirectReason=401` several seconds later for an expired profile. Preflight now waits for a recognizable state and classifies the login URL as `NEEDS_USER / LOGIN_REQUIRED` instead of `ADAPTER_OUTDATED`.
- 2026-09-02: CDP became the preferred transport so the user-owned Chrome and login session remain open across commands. Managed `launchPersistentContext()` remains a fallback.
- 2026-09-02: the current Tiptap editor expanded direct multiline `fill()` calls and the topic button inserted literal `#` characters without a separate search input. The adapter now enters body text line by line and appends one stable plain-text topic line; entity selection remains a reported degradation.
- 2026-09-02: submission controls are rendered inside the closed shadow root of `xhs-publish-btn`. The host exposes the root as `_sr` and carries `submit-disabled` / `save-disabled` state. The adapter first tries semantic locators, then uses the calibrated shadow-root buttons for the exact requested action.
- 2026-09-04: live calibration confirmed inline activity cards, the activity detail drawer and its required topic, platform topic entities, and the delayed date-picker input. A supervised fill selected `一键生成裸眼3D大片`, inserted required topic `裸眼3D图片`, and read back `2026-09-04 14:30` in `Asia/Shanghai` without submitting.

- 2026-09-05: fixed scheduled submission after time selection: the schedule route now uses `scheduleActions` (“定时发布”) with the existing exact semantic/shadow-root lookup. Missing scheduled submission controls return `SCHEDULE_SUBMIT_CONTROL_NOT_FOUND`; immediate publish and draft routes are unchanged. Offline regression cases verify action selection, no fallback, and no submission after time-configuration failure.
