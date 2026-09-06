---
name: xiaohongshu-publish
description: Prepare and deliver Xiaohongshu image posts with automatic relevant/high-potential tag suggestions, optional matching activity selection and required activity tags, plus exact draft, publish-now, or scheduled-publish dispatch from an upstream decision. Use for 小红书一键发布、保存草稿、定时发布、活动匹配、热门话题、发布前校验、失败诊断或重试. Never save or publish without authorization for that exact action.
---

# Xiaohongshu Publish

Use the executable local workflow first. The skill packages validation, browser operations, safety gates, selectors, result codes, retry rules, and examples. Do not repeatedly reason through routine DOM steps when the scripts can handle them.

## Trigger from a multi-platform Agent

For a task already confirmed on a publishing dashboard, the owning Agent verifies its durable approval and execution record, then sends the **unchanged complete task JSON** on stdin to this package's single entry point:

```bash
python3 /absolute/xiaohongshu-publish/scripts/upstream_dispatch.py --output-root /absolute/publish-jobs
```

Optionally add `--node /absolute/node`. The entry locates this skill from its own file; no coordinator import or `--platform`/`--skill-dir` is required. Read [references/llm-integration.md](references/llm-integration.md) for this contract.

The total-control skill collects publishing requirements, returns the frozen task to the Agent and triggers this entry only. **This Xiaohongshu package owns** account/browser checks, image-cover ordering and deduplication, manifest generation and validation, matching topics/activities and required activity topics, preflight, and exact draft/publish/schedule execution. None of those preparations belong in the total-control skill. Existing publisher and matching scripts are reused unchanged.

The entry refuses missing/mismatched approval metadata, changed source bytes or elapsed schedules, and never replays an existing job directory. Its `originSkill=xiaohongshu-publish` receipt identifies which package stopped or completed. Metadata matching is not authorization by itself: copied locator text or arbitrary stdin must never create a fresh approval. Current coverage remains **image posts with explicitly configured public visibility**, not videos.

## Runtime

Run once in this skill directory:

```bash
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install
```

Prefer connecting Playwright to a user-started, visible Chrome through CDP. This keeps Chrome and its login session alive between runs and avoids Playwright launch flags. Start it once with `scripts/start_chrome_cdp.sh /absolute/dedicated-profile`, then use `--cdp-url http://127.0.0.1:9222`. Never expose the debugging port beyond localhost.

`launchPersistentContext()` remains an optional managed-launch fallback through `--profile-dir`. It also persists login when the same directory is reused, but the script owns and closes that Chrome process after each run. Never point either mode at the user's everyday Chrome profile.

## Workflow

1. For direct use (without an upstream frozen task), read [references/payload-schema.md](references/payload-schema.md) and create a manifest from the local post folder. Unless the creator disables it, analyze the title and body for relevant, high-potential topic seeds and set `auto_tags=true`. For an upstream task, the package entry above performs this preparation only after being triggered with valid authorization metadata.
2. Run `validate`. Fix all manifest errors before opening Chrome.
3. Run `preflight` against the CDP browser. Stop with `NEEDS_USER` for login, QR, CAPTCHA, or security checks.
4. During editor fill, rank topic candidates by content relevance first and visible popularity second. When `activity_mode=auto`, inspect the activity widget, select only a sufficiently relevant activity, and merge that activity's required tags ahead of other topics. If no activity matches, skip it and preserve the warning.
5. Match the requested action exactly:
   - inspection only: `validate`, `preflight`, or `verify`;
   - populate editor: `fill --commit`;
   - save platform draft: manifest mode `draft` plus `draft --commit`;
   - schedule: manifest mode `schedule` plus `schedule --commit`;
   - publish now: manifest mode `publish` plus `publish --commit`;
   - upstream decision: `dispatch --decision draft|publish|schedule --commit`. The decision and manifest mode must match exactly or execution stops before the browser opens.
6. Return the script's JSON result without upgrading its meaning. `SUBMITTED` and `UNDER_REVIEW` are not `PUBLISHED`.
7. If selectors fail, use [references/browser-fastpath.md](references/browser-fastpath.md) only to inspect the live DOM and update `assets/selectors.json` or the script. Do not use free-form browser control as the normal path.

## Commands

```bash
node scripts/xhs_publisher.mjs validate --manifest /absolute/post.json
scripts/start_chrome_cdp.sh /absolute/xhs-profile
node scripts/xhs_publisher.mjs preflight --cdp-url http://127.0.0.1:9222
node scripts/xhs_publisher.mjs fill --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs draft --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs schedule --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs publish --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs dispatch --decision draft --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs dispatch --decision publish --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs dispatch --decision schedule --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
node scripts/xhs_publisher.mjs verify --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222
```

`--commit` authorizes browser mutation for that command. It is deliberately required for `fill` too, because an editor may auto-save. It never authorizes a different command. In CDP mode Chrome remains open after the command, so `--keep-open` is normally unnecessary.

## Core rules

- Never ask for or store a password, export cookies, bypass CAPTCHA, call private APIs, or reuse the ordinary Chrome data directory.
- Never infer publish authorization from “准备好素材”, “试一下”, “填进去”, or prior authorization for a different post.
- Treat the click on `发布` as irreversible. After an uncertain click, run `verify`; do not click again automatically.
- Keep the local manifest and assets as the source of truth. A platform draft is a delivery state, not the canonical document.
- Treat an upstream `draft`, `publish`, or `schedule` decision literally. Never substitute one route for another or infer a missing decision.
- Generate content-relevant tag candidates automatically. Prefer platform topic entities and visible popularity signals; if the UI cannot expose a topic entity, preserve the literal hashtag fallback warning.
- Never select an activity for popularity alone. Auto-select only when it is relevant to the title/body/tags. Merge required activity tags before other topics. If no candidate clears the relevance threshold, continue without an activity.
- After the scheduled time is entered and read back, use `signals.scheduleActions` to match the enabled “定时发布” submit button exactly. Do not substitute the immediate “发布” action.
- A scheduled job must preflight again shortly before dispatch. Session expiry and platform challenges can still require the creator.
- Update selector configuration only after live inspection; add a regression note to the examples when behavior changes.

Read [references/rules.md](references/rules.md) for authorization, idempotency, status, retry, and failure rules. Read [references/examples.md](references/examples.md) for valid calls, anti-examples, and boundary cases. Read [references/platform-notes.md](references/platform-notes.md) only for platform constraints and transport rationale.

For third-party LLM function calling, use the strict schema and stdin bridge described in [references/llm-integration.md](references/llm-integration.md). Keep authorization enforcement outside the model.
