# LLM integration contract

Use this reference when connecting a Chinese or other third-party LLM API to the local runtime.

## Frozen task from a publishing coordinator

Use `scripts/upstream_dispatch.py` as the single package entry when an owning Agent hands off a dashboard-confirmed task. Invoke it with an argument array and JSON stdin:

```text
python3 /absolute/xiaohongshu-publish/scripts/upstream_dispatch.py --output-root /absolute/publish-jobs [--node /absolute/node]
```

The Agent must first read the persistent approval and execution record and verify the plan hash, current material version and non-replayed job state. It forwards the original object, including:

- `sessionId`, `planId`, `planHash`, stable `jobId`, `platform="xiaohongshu"`, exact `decision="draft"|"publish"|"schedule"`;
- `accountProfile`, original `accountSettings`, `source`, `scheduledAt`, `timezone`, `scheduleUtc`, `draftScope`, `acceptLocalDraft`, `policies`;
- `authorization` with nonempty `receiptId`, matching `sessionId`/`planId`/`planHash`, `assetRevision`, `sourceHash`, and `confirmationSource="dashboard"`.

`source` contains `assetRevision`, `media.kind="image_post"`, `media.files`, and `metadata.title/body/cover`. Each file record contains its stored absolute `path`, integer byte `size` and SHA-256 `sha256`; cover is either such a record or null. `authorization.sourceHash` is the SHA-256 of UTF-8 canonical JSON for `source` (sorted keys, no extra spaces, unescaped Unicode). The entry verifies both the source record and actual bytes; it does not infer roles from filenames or alter the approved text.

The entry checks authorization metadata **before any preparation**, reserves `<output-root>/<jobId>` once, then generates the local manifest. It uses the explicit cover as first image, removes media entries byte-identical to that cover, and preserves all other image order. Account/browser setup, validation, topic/activity matching and browser actions are all internal to this package. For schedules, it preserves the approved instant and confirms the supplied timezone and UTC refer to that same instant; the existing publisher owns conversion into the browser's timezone.

The coordinator/dashboard must not make a Xiaohongshu manifest, sort cover images, check login, or call `validate`/`preflight`/`dispatch` itself. It triggers this package and displays the single returned JSON object. The package exposes `dispatch_request(args, request)` for isolated tests; subprocess invocations should be mocked in such tests.

All returned results include `status`, `outcome`, `message` and `originSkill="xiaohongshu-publish"`. `SUBMITTED` and `UNDER_REVIEW` do not imply publicly published. `JOB_ALREADY_ATTEMPTED`, an uncertain result, or a paused job must not be automatically retried or worked around by changing `jobId`. The entry is a trusted local capability, not a signed-receipt verifier: stdin metadata alone cannot authenticate a user, and must not be exposed as a public API or model-minted authorization.

## Responsibility split

- The LLM maps the user's intent to a command and prepares or selects a local manifest.
- The application owns authorization state. It must set `commit: true` only when the user explicitly authorized that exact mutating command for the current post.
- For the three-route dispatcher, send `command=dispatch`, `decision=draft|publish|schedule`, and a manifest whose `mode` is the same value. A missing or mismatched decision is a hard failure.
- `scripts/xhs_tool_bridge.mjs` validates the tool-call envelope and invokes the deterministic publisher without a shell.
- `scripts/xhs_publisher.mjs` owns manifest validation, Playwright actions, and machine-readable results.
- The LLM must not invent selectors, execute arbitrary shell strings, receive cookies, or decide that an ambiguous result is success.
- For browser commands, provide `cdp_url` and leave `profile_dir` null. Use `profile_dir` only for the managed-launch fallback; never provide both.

## Tool definition

Expose `assets/tool-schema.json` through the model provider's function/tool-calling format. If the provider uses a different wrapper, preserve the function name, enums, required fields, and `additionalProperties: false` behavior.

Pass the returned arguments as JSON on standard input:

```bash
printf '%s' "$TOOL_ARGUMENTS_JSON" | node scripts/xhs_tool_bridge.mjs
```

In application code, prefer a subprocess API with an argument array and standard input. Do not interpolate model output into a shell command.

## Result handling

- Parse standard output as one JSON object.
- Continue automatically only when `ok` is `true` and the status is the one expected for the command.
- Surface `NEEDS_AUTHORIZATION` and `NEEDS_USER` to the user.
- Route `ADAPTER_OUTDATED` to adapter maintenance.
- Route `UNKNOWN` to manual note-manager inspection; never retry submission automatically.
- Store the result beside the local post as an append-only execution record if audit history is needed. No database is required.

## Minimal application loop

1. Load `post.md` and the manifest from one post folder.
2. Ask the LLM for a structured tool call only when natural-language intent must be interpreted.
3. Enforce authorization and exact decision/mode equality in application code independently of the model.
4. Invoke the bridge and render its JSON result.
5. For a future scheduled run, invoke `preflight` shortly before dispatch, then invoke `dispatch` with `decision=schedule` only if the stored task explicitly permits that exact time and post.
