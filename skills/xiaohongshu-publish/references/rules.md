# Execution and safety rules

## Authorization boundary

Commands are capabilities, not synonyms:

| Command | External effect | Required authorization |
| --- | --- | --- |
| `validate` | Reads local manifest and images | None beyond the request |
| `preflight` | Opens creator center and reads session state | Permission to inspect the signed-in session |
| `verify` | Reads note manager | Permission to inspect the account |
| `fill` | Uploads media and edits the web form; auto-save may occur | Explicit request to fill or preview this post, plus `--commit` |
| `draft` | Attempts to create a platform draft | Explicit request to save this post as draft, plus `--commit` |
| `schedule` | Submits a scheduled post | Explicit request to schedule this post and time, plus `--commit` |
| `publish` | Submits immediately | Explicit request to publish this post now, plus `--commit` |
| `dispatch --decision draft\|publish\|schedule` | Executes exactly one upstream route | Exact decision, matching manifest mode, plus `--commit` |

Authorization is scoped to the current post, account, mode, and turn. A request to test, inspect, validate, prepare, or open the page is not authorization to save or submit. Draft authorization is not publish authorization.

For `dispatch`, reject a missing decision and reject any mismatch between `--decision` and manifest `mode` before opening Chrome. Never reinterpret either value.

## State machine

`VALID` → `PREFLIGHT_OK` → `FILLED` → one of `DRAFT_SAVED`, `SCHEDULED`, `SUBMITTED`, `UNDER_REVIEW`, or `PUBLISHED`.

Stop states are `NEEDS_AUTHORIZATION`, `NEEDS_USER`, `VALIDATION_FAILED`, `FILL_FAILED`, `SCHEDULE_FAILED`, `ADAPTER_OUTDATED`, `UNKNOWN`, `REJECTED`, and `EXECUTION_FAILED`. Never report a stop state as success.

## Success evidence

- `FILLED`: title and body were read back from the editor. For a schedule manifest it also means the timezone-adjusted time was written and read back exactly. It does not mean draft saved or submitted.
- `DRAFT_SAVED`: note manager contains the expected title after the draft action.
- `SCHEDULED`: note manager contains the expected title after scheduled submission. When the UI exposes the scheduled time, a future adapter should compare it too.
- `SUBMITTED`: note manager contains the title, but publication is not yet confirmed.
- `UNDER_REVIEW`: the title is present with a review state.
- `PUBLISHED`: the title is present with a published state.
- `UNKNOWN`: the action may have happened but verification is insufficient. Human review is required before retry.

## Idempotency and retry

- Keep `job_id` stable for the same logical post. The validator calculates `content_fingerprint` from normalized fields and image bytes.
- Before retrying after an uncertain draft or submit action, run `verify` and inspect the note manager.
- Retry transient navigation or network failures at most once only before the irreversible action.
- Never auto-retry after clicking publish/schedule, after `UNKNOWN`, or after platform policy/review rejection.
- Never change title, visibility, schedule, an exact activity, or account merely to make an automated run succeed.
- Auto activity matching may skip unavailable or weakly related activities. Exact activity matching must stop if the requested activity is unavailable.
- Rank topics and activities by content relevance before popularity. Popular but unrelated candidates are not acceptable.

## Login and profile

- Use a dedicated persistent Chrome user-data directory, for example `/Users/name/.epost/chrome/xiaohongshu`.
- Prefer a long-running Chrome started with a localhost remote-debugging port and connect through CDP. The CLI disconnects without closing this browser.
- Pass `--remote-debugging-port` together with the dedicated `--user-data-dir`. Never expose the CDP port to the LAN or internet.
- Use `--profile-dir` only for the managed-launch fallback. Reusing the same directory persists login, but the CLI closes the launched Chrome after each command.
- Never open two Chrome processes with the same dedicated directory.
- Login, QR confirmation, CAPTCHA, security checks, and account recovery remain human steps.

## Adapter maintenance

- UI constraints and labels are observations, not a contract. Keep likely strings in `assets/selectors.json` and structural logic in the script.
- On `ADAPTER_OUTDATED`, inspect the current DOM without committing an action, update the smallest stable selector set, and run validation tests.
- Do not use private HTTP endpoints, copied cookies, reverse-engineered signatures, or CAPTCHA bypass as fallbacks.

## Logs and sensitive data

- JSON results may record job id, fingerprint, title, status, URL, warning codes, and screenshot path.
- Do not log passwords, cookies, local-storage tokens, QR payloads, or the full Chrome profile.
- Screenshots can contain account information and unpublished content; store them under the local post folder and treat them as private.
