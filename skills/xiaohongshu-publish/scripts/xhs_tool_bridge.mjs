#!/usr/bin/env node

import {spawnSync} from "node:child_process";
import {readFileSync} from "node:fs";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const publisher = resolve(scriptDir, "xhs_publisher.mjs");
const allowed = new Set(["validate", "preflight", "fill", "draft", "schedule", "publish", "dispatch", "verify"]);

function fail(error) {
  process.stdout.write(`${JSON.stringify({
    ok: false,
    status: "TOOL_INPUT_FAILED",
    error,
    retryable: false,
  }, null, 2)}\n`);
  process.exit(2);
}

let input;
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch (error) {
  fail(`invalid_json:${error.message}`);
}

if (!input || typeof input !== "object" || Array.isArray(input)) fail("input_must_be_object");
if (!allowed.has(input.command)) fail("invalid_command");
if (input.command === "dispatch" && !new Set(["draft", "publish", "schedule"]).has(input.decision)) {
  fail("dispatch_requires_draft_publish_or_schedule_decision");
}
if (input.command !== "dispatch" && input.decision != null) fail("decision_valid_only_for_dispatch");
if (input.command !== "preflight" && typeof input.manifest !== "string") fail("manifest_required");
const mutating = new Set(["fill", "draft", "schedule", "publish", "dispatch"]);
if (mutating.has(input.command) && input.commit !== true) fail(`${input.command}_requires_commit_true`);
if (!mutating.has(input.command) && input.commit === true) fail("commit_true_valid_only_for_mutating_commands");
if (input.command !== "validate") {
  const hasProfile = typeof input.profile_dir === "string";
  const hasCdp = typeof input.cdp_url === "string";
  if (hasProfile === hasCdp) fail("provide_exactly_one_of_profile_dir_or_cdp_url");
}

const args = [publisher, input.command];
if (input.manifest) args.push("--manifest", input.manifest);
if (input.profile_dir) args.push("--profile-dir", input.profile_dir);
if (input.cdp_url) args.push("--cdp-url", input.cdp_url);
if (input.command === "dispatch") args.push("--decision", input.decision);
if (input.commit === true) args.push("--commit");
if (input.keep_open === true) args.push("--keep-open");
if (typeof input.screenshot === "string") args.push("--screenshot", input.screenshot);

const completed = spawnSync(process.execPath, args, {
  encoding: "utf8",
  stdio: ["ignore", "pipe", "pipe"],
});

if (completed.stdout) process.stdout.write(completed.stdout);
if (completed.stderr) process.stderr.write(completed.stderr);
if (!completed.stdout) fail(completed.error?.message || "publisher_returned_no_result");
process.exit(completed.status ?? 1);
