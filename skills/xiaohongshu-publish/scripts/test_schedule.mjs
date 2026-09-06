#!/usr/bin/env node

// Exercise the actual submission function offline; no browser or platform calls.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("./xhs_publisher.mjs", import.meta.url), "utf8");
const selectors = JSON.parse(readFileSync(new URL("../assets/selectors.json", import.meta.url), "utf8"));
const start = source.indexOf("async function executeWrite(");
assert(start >= 0);
const end = source.indexOf("\n}\n", start) + 2;
assert(end > start);
const implementation = source.slice(start, end);

async function run(command, availableLabels, fillResult = {ok: true, warnings: []}) {
  const clicks = [];
  const events = [];
  const sandbox = {
    selectorConfig: selectors,
    result: (status, details) => ({ok: false, status, ...details}),
    applyPost: async () => { events.push("time-and-content-readback"); return fillResult; },
    findAction: async (_page, labels) => {
      events.push("find-submit");
      const label = labels.find(item => availableLabels.includes(item));
      return label ? {click: async () => { clicks.push(label); events.push("click"); }} : null;
    },
    verifyInManager: async (_page, _manifest, kind) => {
      events.push("verify");
      return {ok: true, kind};
    },
  };
  vm.runInNewContext(`${implementation}\nglobalThis.submit = executeWrite;`, sandbox);
  const result = await sandbox.submit({waitForTimeout: async () => {}}, command, {mode: command});
  return {result, clicks, events};
}

const scheduled = await run("schedule", ["发布", "定时发布", "保存草稿"]);
assert.deepEqual(scheduled.clicks, ["定时发布"]);
assert.deepEqual(scheduled.events, ["time-and-content-readback", "find-submit", "click", "verify"]);
assert.equal(scheduled.result.kind, "schedule");

const scheduledOnly = await run("schedule", ["定时发布"]);
assert.deepEqual(scheduledOnly.clicks, ["定时发布"]);

const missing = await run("schedule", ["发布", "保存草稿"]);
assert.deepEqual(missing.clicks, []);
assert.equal(missing.result.status, "ADAPTER_OUTDATED");
assert.equal(missing.result.error, "SCHEDULE_SUBMIT_CONTROL_NOT_FOUND");

const failedTime = await run("schedule", ["定时发布"], {ok: false, status: "SCHEDULE_FAILED"});
assert.deepEqual(failedTime.clicks, []);
assert.deepEqual(failedTime.events, ["time-and-content-readback"]);
assert.equal(failedTime.result.status, "SCHEDULE_FAILED");

const immediate = await run("publish", ["发布", "定时发布"]);
assert.deepEqual(immediate.clicks, ["发布"]);
const draft = await run("draft", ["发布", "定时发布", "保存草稿"]);
assert.deepEqual(draft.clicks, ["保存草稿"]);

process.stdout.write(JSON.stringify({event: "schedule_submission_tests_passed", cases: 6, platform_calls: 0}) + "\n");
