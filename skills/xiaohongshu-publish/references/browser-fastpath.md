# Live-browser recovery path

Read this only when the Playwright runtime returns `ADAPTER_OUTDATED`, or when initially calibrating selectors. It is a maintenance procedure, not the normal publishing engine.

Use the Chrome control skill to inspect the creator center's visible state. Keep one claimed tab for the transaction. Prefer semantic roles, labels, placeholders, and stable attributes over generated CSS classes. Record confirmed changes in `assets/selectors.json` and `scripts/xhs_publisher.mjs`.

## Preflight

1. Open the Xiaohongshu creator center and verify that the note manager or publisher is visible.
2. Fail preflight on a login page, QR prompt, CAPTCHA, security dialog, missing creator permission, or incompatible editor.
3. Confirm the requested media count, title length, body length, visibility, activity, and schedule can be represented before submission.

## Apply the post

1. Navigate to the image-post publisher.
2. Start the file-chooser wait before activating the upload control. Set all absolute image paths at once when the current chooser supports multiple files; otherwise add them sequentially and verify the final count and order.
3. Fill title and body once. Preserve intentional newlines.
4. Insert topics through the platform suggestion list. Rank by content relevance first and visible popularity second. Record a degraded warning if only literal hashtag text can be inserted.
5. Set visibility. For automatic activity matching, select only a sufficiently relevant visible candidate, then capture and merge any required activity topics. If no activity matches, skip it. For exact mode, stop rather than substitute.
6. For `schedule`, use the live platform minimum lead-time rule. Never silently convert a rejected schedule into immediate publication.
7. For `draft`, locate the actual visible save-draft control. Do not assume it is named `暂存离开`, and do not treat editor auto-save as a verified platform draft. Verify the title in the note manager's draft area. For `publish` or `schedule`, activate the confirmed submission control once.

## Verify and retry

- Treat the click on `发布` as the irreversible boundary.
- After that boundary, do not click again on timeout. Open the note manager and search for the title and submission time.
- Return `SUBMITTED` when the platform accepted the post, `UNDER_REVIEW` when review is pending, and `PUBLISHED` only when the note manager confirms publication.
- If the editor changed before the submit boundary, refresh once, restore the normalized manifest, and retry once. Stop after the second structural mismatch and report `ADAPTER_OUTDATED`.
- If no explicit draft control exists in the current UI, stop with `ADAPTER_OUTDATED`; do not substitute closing the page, navigating away, or publishing.
