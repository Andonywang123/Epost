#!/usr/bin/env node

import {spawnSync} from "node:child_process";
import {existsSync, readFileSync} from "node:fs";
import {homedir} from "node:os";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {chromium} from "playwright";
import {
  activityContext,
  chooseCandidate,
  deriveContentTags,
  extractRequiredTags,
  mergeTags,
  topicEntityName,
} from "./xhs_matching.mjs";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const skillDir = resolve(scriptDir, "..");
const selectorConfig = JSON.parse(
  readFileSync(resolve(skillDir, "assets/selectors.json"), "utf8"),
);

const WRITE_COMMANDS = new Set(["fill", "draft", "schedule", "publish", "dispatch"]);
const COMMANDS = new Set(["validate", "preflight", "fill", "draft", "schedule", "publish", "dispatch", "verify"]);
const SUCCESS_STATUSES = new Set([
  "VALID",
  "PREFLIGHT_OK",
  "FILLED",
  "SCHEDULE_CONFIGURED",
  "DRAFT_SAVED",
  "SUBMITTED",
  "UNDER_REVIEW",
  "PUBLISHED",
  "SCHEDULED",
]);
let attachedOverCdp = false;

function usage() {
  return `Usage:
  node scripts/xhs_publisher.mjs validate --manifest /absolute/post.json
  node scripts/xhs_publisher.mjs preflight --cdp-url http://127.0.0.1:9222
  node scripts/xhs_publisher.mjs fill --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
  node scripts/xhs_publisher.mjs draft --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
  node scripts/xhs_publisher.mjs schedule --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
  node scripts/xhs_publisher.mjs publish --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
  node scripts/xhs_publisher.mjs dispatch --decision draft|publish|schedule --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222 --commit
  node scripts/xhs_publisher.mjs verify --manifest /absolute/post.json --cdp-url http://127.0.0.1:9222

Use --profile-dir /absolute/chrome-profile instead of --cdp-url only for managed-launch fallback.`;
}

function parseArgs(argv) {
  const [command, ...rest] = argv;
  const options = {};
  for (let index = 0; index < rest.length; index += 1) {
    const value = rest[index];
    if (!value.startsWith("--")) {
      throw new Error(`unexpected_argument:${value}`);
    }
    const raw = value.slice(2);
    if (raw.includes("=")) {
      const [key, ...parts] = raw.split("=");
      options[key] = parts.join("=");
      continue;
    }
    const next = rest[index + 1];
    if (next && !next.startsWith("--")) {
      options[raw] = next;
      index += 1;
    } else {
      options[raw] = true;
    }
  }
  return {command, options};
}

function expandPath(value) {
  if (!value) return value;
  if (value === "~") return homedir();
  if (value.startsWith("~/")) return resolve(homedir(), value.slice(2));
  return resolve(value);
}

function result(status, extra = {}) {
  return {
    ok: SUCCESS_STATUSES.has(status),
    status,
    time: new Date().toISOString(),
    ...extra,
  };
}

function emit(value) {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

function validationError(stdout, stderr) {
  try {
    const parsed = JSON.parse(stdout.trim());
    return result("VALIDATION_FAILED", {
      error: parsed.error || "manifest_validation_failed",
      details: parsed,
      retryable: false,
    });
  } catch {
    return result("VALIDATION_FAILED", {
      error: stderr.trim() || stdout.trim() || "validator_failed",
      retryable: false,
    });
  }
}

function validateManifest(manifestPath) {
  if (!manifestPath) {
    throw new Error("manifest_required");
  }
  const absoluteManifest = expandPath(manifestPath);
  if (!existsSync(absoluteManifest)) {
    throw new Error(`manifest_not_found:${absoluteManifest}`);
  }
  const validator = resolve(scriptDir, "xhs_payload.py");
  const completed = spawnSync("python3", [validator, "validate", "--manifest", absoluteManifest], {
    encoding: "utf8",
  });
  if (completed.status !== 0) {
    const error = validationError(completed.stdout, completed.stderr);
    const wrapped = new Error("manifest_validation_failed");
    wrapped.details = error;
    throw wrapped;
  }
  return JSON.parse(completed.stdout).payload;
}

async function firstVisible(locators) {
  for (const locator of locators) {
    const count = await locator.count();
    for (let index = 0; index < count; index += 1) {
      const candidate = locator.nth(index);
      if (await candidate.isVisible()) return candidate;
    }
  }
  return null;
}

async function visibleText(page) {
  return page.locator("body").innerText({timeout: 15000});
}

function includesAny(text, signals) {
  return signals.some((signal) => text.includes(signal));
}

async function goto(page, url) {
  let lastError;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await page.goto(url, {waitUntil: "domcontentloaded", timeout: 30000});
      await page.waitForTimeout(800);
      return;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError;
}

async function preflight(page) {
  await goto(page, selectorConfig.urls.home);
  let text = "";
  for (let attempt = 0; attempt < 16; attempt += 1) {
    if (new URL(page.url()).pathname.startsWith("/login")) {
      return result("NEEDS_USER", {
        error: "LOGIN_REQUIRED",
        retryable: false,
        url: page.url(),
      });
    }
    text = await visibleText(page);
    const recognized = [
      ...selectorConfig.signals.needsUser,
      ...selectorConfig.signals.needsLogin,
      ...selectorConfig.signals.loggedIn,
    ];
    if (includesAny(text, recognized)) break;
    await page.waitForTimeout(500);
  }
  if (includesAny(text, selectorConfig.signals.needsUser)) {
    return result("NEEDS_USER", {
      error: "SECURITY_CHALLENGE",
      retryable: false,
      url: page.url(),
    });
  }
  if (includesAny(text, selectorConfig.signals.needsLogin)) {
    return result("NEEDS_USER", {
      error: "LOGIN_REQUIRED",
      retryable: false,
      url: page.url(),
    });
  }
  if (!includesAny(text, selectorConfig.signals.loggedIn)) {
    return result("ADAPTER_OUTDATED", {
      error: "CREATOR_CENTER_NOT_RECOGNIZED",
      retryable: false,
      url: page.url(),
    });
  }
  const accountId = text.match(/小红书账号\s*[:：]?\s*(\d{5,})/)?.[1] ?? null;
  return result("PREFLIGHT_OK", {
    account_id: accountId,
    url: page.url(),
  });
}

async function fillRichText(page, editor, body) {
  await editor.click();
  await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A");
  await page.keyboard.press("Backspace");
  const lines = body.split("\n");
  for (let index = 0; index < lines.length; index += 1) {
    if (lines[index]) await page.keyboard.insertText(lines[index]);
    if (index < lines.length - 1) await page.keyboard.press("Enter");
  }
}

async function placeCaretAtEnd(editor) {
  await editor.evaluate((element) => {
    element.focus();
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(element);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
  });
}

async function configuredCandidates(page, key) {
  const selectors = selectorConfig.selectors[key] ?? [];
  const candidates = [];
  for (const selector of selectors) {
    const locator = page.locator(selector);
    const count = Math.min(await locator.count(), 80);
    for (let index = 0; index < count; index += 1) {
      const item = locator.nth(index);
      if (!await item.isVisible().catch(() => false)) continue;
      const text = (await item.innerText().catch(() => "")).trim();
      if (text.length < 2 || text.length > 240) continue;
      candidates.push({text, locator: item});
    }
  }
  return candidates;
}

async function firstConfiguredInput(page, key) {
  const selectors = selectorConfig.selectors[key] ?? [];
  return firstVisible(selectors.map((selector) => page.locator(selector)));
}

async function waitConfiguredCandidates(page, key, timeoutMs = 4000) {
  const deadline = Date.now() + timeoutMs;
  do {
    const candidates = await configuredCandidates(page, key);
    if (candidates.length) return candidates;
    await page.waitForTimeout(250);
  } while (Date.now() < deadline);
  return [];
}

async function editorTopicNames(editor) {
  return editor.locator("a.tiptap-topic").evaluateAll((anchors) => anchors.map((anchor) => {
    try {
      return JSON.parse(anchor.getAttribute("data-topic") || "{}").name
        || anchor.textContent.replace(/\[话题\]#/g, "").replace(/^#/, "").trim();
    } catch {
      return anchor.textContent.replace(/\[话题\]#/g, "").replace(/^#/, "").trim();
    }
  }).filter(Boolean));
}

async function cleanDuplicateTopicPrefixes(page, editor) {
  let cleaned = 0;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const found = await editor.evaluate((element) => {
      for (const anchor of element.querySelectorAll("a.tiptap-topic")) {
        const previous = anchor.previousSibling;
        if (!previous || previous.nodeType !== Node.TEXT_NODE) continue;
        const match = previous.data.match(/(?:[#＃][\s\u00a0]*)+$/u);
        if (!match) continue;
        const selection = window.getSelection();
        const range = document.createRange();
        range.setStart(previous, previous.data.length - match[0].length);
        range.setEnd(previous, previous.data.length);
        selection.removeAllRanges();
        selection.addRange(range);
        return true;
      }
      return false;
    });
    if (!found) break;
    await page.keyboard.press("Backspace");
    cleaned += 1;
  }
  return cleaned;
}

async function clearPendingTopicSuggestion(page, editor) {
  const found = await editor.evaluate((element) => {
    const suggestions = [...element.querySelectorAll("span.suggestion")];
    const suggestion = suggestions.at(-1);
    if (!suggestion) return false;
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNode(suggestion);
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  });
  if (found) await page.keyboard.press("Backspace");
}

async function cleanStandaloneHashRuns(page, editor) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const found = await editor.evaluate((element) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (!/[#＃]/u.test(node.data) || !/^[#＃\s\u00a0]+$/u.test(node.data)) continue;
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(node);
        selection.removeAllRanges();
        selection.addRange(range);
        return true;
      }
      return false;
    });
    if (!found) break;
    await page.keyboard.press("Backspace");
  }
}

async function selectTopicEntity(page, editor, tag) {
  await placeCaretAtEnd(editor);
  const before = await editorTopicNames(editor);
  const configuredTrigger = selectorConfig.selectors.topicTrigger
    ? await firstVisible([page.locator(selectorConfig.selectors.topicTrigger)])
    : null;
  const trigger = configuredTrigger
    ?? await findAction(page, selectorConfig.signals.topicActions ?? []);
  if (!trigger) return null;
  await trigger.click({timeout: 3000});
  await page.waitForTimeout(350);
  const search = await firstConfiguredInput(page, "topicSearchInputs");
  if (search) {
    await search.fill(tag);
    await page.waitForTimeout(450);
  }
  const candidates = await waitConfiguredCandidates(page, "topicCandidates", 3000);
  const best = chooseCandidate(tag, candidates, {minimumScore: 2});
  if (!best) {
    await clearPendingTopicSuggestion(page, editor);
    await page.keyboard.press("Escape");
    return null;
  }
  const selectedName = topicEntityName(best.candidate.text) || tag;
  const escapedName = selectedName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const stableCandidate = page.locator(".recommend-topic-wrapper .tag")
    .filter({hasText: new RegExp(`^#?${escapedName}(?:\\s|$)`, "u")}).first();
  const clickTarget = await stableCandidate.isVisible().catch(() => false)
    ? stableCandidate
    : best.candidate.locator;
  await clickTarget.click({timeout: 3000});
  await page.waitForTimeout(200);
  const after = await editorTopicNames(editor);
  return after.length > before.length ? after[after.length - 1] : null;
}

async function appendLiteralTopics(page, editor, tags) {
  if (!tags.length) return;
  await placeCaretAtEnd(editor);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await page.keyboard.insertText(tags.map((tag) => `#${tag}`).join(" "));
}

async function insertTopics(page, editor, body, tags) {
  if (!tags.length) return {warnings: [], inserted: [], fallback: []};
  const inserted = [];
  const fallback = [];
  for (const tag of tags) {
    const currentText = await editor.innerText().catch(() => "");
    if (currentText.includes(`#${tag}`)) {
      inserted.push(tag);
      continue;
    }
    const selected = await selectTopicEntity(page, editor, tag).catch(async () => {
      await clearPendingTopicSuggestion(page, editor).catch(() => null);
      await page.keyboard.press("Escape").catch(() => null);
      return null;
    });
    if (selected) inserted.push(selected);
    else fallback.push(tag);
  }
  if (!inserted.length) {
    await fillRichText(page, editor, body);
  }
  await cleanDuplicateTopicPrefixes(page, editor);
  await cleanStandaloneHashRuns(page, editor);
  const actualEntities = mergeTags(await editorTopicNames(editor));
  const actualEntityKeys = new Set(actualEntities.map((tag) => tag.toLocaleLowerCase("zh-CN")));
  const remainingFallback = fallback.filter(
    (tag) => !actualEntityKeys.has(tag.toLocaleLowerCase("zh-CN")),
  );
  await appendLiteralTopics(page, editor, remainingFallback);
  const warnings = [];
  if (remainingFallback.length) {
    warnings.push({
      code: "TOPICS_AS_TEXT",
      message: "部分话题未能选中平台实体，已保留为普通话题文本。",
      tags: remainingFallback,
    });
  }
  return {warnings, inserted: actualEntities, fallback: remainingFallback};
}

async function selectActivity(page, manifest) {
  if (manifest.activity_mode === "none") {
    return {ok: true, selected: null, requiredTags: [], warnings: []};
  }
  let candidates = await waitConfiguredCandidates(page, "activityCandidates", 5000);
  if (!candidates.length) {
    const trigger = await findAction(page, selectorConfig.signals.activityActions ?? []);
    if (trigger) {
      await trigger.click();
      await page.waitForTimeout(450);
      candidates = await waitConfiguredCandidates(page, "activityCandidates", 2500);
    }
  }
  if (!candidates.length) {
    if (manifest.activity_mode === "exact") {
      return {ok: false, output: result("ADAPTER_OUTDATED", {
        error: "ACTIVITY_WIDGET_NOT_FOUND",
        requested_activity: manifest.activity,
        retryable: false,
      })};
    }
    return {ok: true, selected: null, requiredTags: [], warnings: [{
      code: "ACTIVITY_SKIPPED_WIDGET_NOT_FOUND",
      message: "未找到可用的活动小组件，已按规则跳过活动。",
    }]};
  }
  const best = chooseCandidate(activityContext(manifest), candidates, {
    exactName: manifest.activity_mode === "exact" ? manifest.activity : null,
    minimumScore: 4,
  });
  if (!best) {
    await page.keyboard.press("Escape");
    if (manifest.activity_mode === "exact") {
      return {ok: false, output: result("VALIDATION_FAILED", {
        error: "REQUESTED_ACTIVITY_NOT_AVAILABLE",
        requested_activity: manifest.activity,
        retryable: false,
      })};
    }
    return {ok: true, selected: null, requiredTags: [], warnings: [{
      code: "ACTIVITY_SKIPPED_NO_MATCH",
      message: "当前活动候选中没有足够相关的项目，未选活动。",
    }]};
  }
  const selectedName = (await best.candidate.locator.locator(".activity-name").first()
    .innerText().catch(() => "")) || best.candidate.text.split("\n")[0].trim();
  const requiredTags = extractRequiredTags(best.candidate.text);
  const detailTarget = await firstVisible([
    best.candidate.locator.locator(".activity-card-main"),
    best.candidate.locator,
  ]);
  await detailTarget.click();
  await page.waitForTimeout(650);
  const detail = await firstVisible((selectorConfig.selectors.activityDetails ?? [])
    .map((selector) => page.locator(selector)));
  if (detail) {
    requiredTags.push(...extractRequiredTags(await detail.innerText().catch(() => "")));
    const associate = await firstVisible([
      detail.getByRole("button", {name: "关联活动", exact: true}),
      detail.getByText("关联活动", {exact: true}),
    ]);
    if (!associate) {
      return {ok: false, output: result("ADAPTER_OUTDATED", {
        error: "ACTIVITY_ASSOCIATE_CONTROL_NOT_FOUND",
        selected_activity: selectedName,
        retryable: false,
      })};
    }
    await associate.click();
    await page.waitForTimeout(350);
    const close = detail.locator(".d-drawer-close").first();
    if (await close.isVisible().catch(() => false)) {
      await close.click({timeout: 3000});
    }
  } else {
    const associate = await firstVisible([
      best.candidate.locator.getByRole("button", {name: "关联", exact: true}),
      best.candidate.locator.getByText("关联", {exact: true}),
    ]);
    if (!associate) {
      return {ok: false, output: result("ADAPTER_OUTDATED", {
        error: "ACTIVITY_ASSOCIATE_CONTROL_NOT_FOUND",
        selected_activity: selectedName,
        retryable: false,
      })};
    }
    await associate.click();
  }
  await page.waitForTimeout(500);
  const requirementTexts = await configuredCandidates(page, "activityRequiredTags");
  requiredTags.push(...requirementTexts.flatMap((candidate) => extractRequiredTags(candidate.text)));
  return {
    ok: true,
    selected: selectedName,
    requiredTags: mergeTags(requiredTags),
    warnings: [],
  };
}

function normalizedEditorText(value) {
  return value.replace(/\r/g, "").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

function formatDateTimeInZone(value, timeZone) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(new Date(value)).filter((part) => part.type !== "literal")
    .map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

async function applyPost(page, manifest) {
  await goto(page, selectorConfig.urls.imagePublisher);
  await page.keyboard.press("Escape");
  const pageText = await visibleText(page);
  if (includesAny(pageText, selectorConfig.signals.needsUser)) {
    return result("NEEDS_USER", {error: "SECURITY_CHALLENGE", retryable: false});
  }
  if (includesAny(pageText, selectorConfig.signals.needsLogin)) {
    return result("NEEDS_USER", {error: "LOGIN_REQUIRED", retryable: false});
  }

  const fileInput = page.locator(selectorConfig.selectors.fileInput).first();
  await fileInput.waitFor({state: "attached", timeout: 15000});
  await fileInput.setInputFiles(manifest.images);

  const titleInput = page.locator(selectorConfig.selectors.titleInput).first();
  await titleInput.waitFor({state: "visible", timeout: 30000});
  await titleInput.fill(manifest.title);

  const bodyEditor = await firstVisible([
    page.locator(selectorConfig.selectors.bodyEditor),
  ]);
  if (!bodyEditor) {
    return result("ADAPTER_OUTDATED", {
      error: "BODY_EDITOR_NOT_FOUND",
      retryable: false,
    });
  }
  await fillRichText(page, bodyEditor, manifest.body);

  const activitySelection = await selectActivity(page, manifest);
  if (!activitySelection.ok) return activitySelection.output;
  if (activitySelection.requiredTags.length > manifest.tag_limit) {
    return result("VALIDATION_FAILED", {
      error: "ACTIVITY_REQUIRED_TAGS_EXCEED_LIMIT",
      required_tags: activitySelection.requiredTags,
      tag_limit: manifest.tag_limit,
      retryable: false,
    });
  }

  const seedTags = mergeTags(manifest.tags, manifest.hot_tag ? [manifest.hot_tag] : []);
  const discoveredTags = manifest.auto_tags
    ? deriveContentTags(manifest.title, manifest.body, seedTags, manifest.tag_limit)
    : seedTags;
  const topics = mergeTags(activitySelection.requiredTags, discoveredTags)
    .slice(0, manifest.tag_limit);
  const topicInsertion = await insertTopics(page, bodyEditor, manifest.body, topics);
  const warnings = [...activitySelection.warnings, ...topicInsertion.warnings];
  await page.keyboard.press("Escape");

  if (manifest.visibility !== "公开") {
    return result("ADAPTER_OUTDATED", {
      error: "NON_PUBLIC_VISIBILITY_NOT_CALIBRATED",
      requested_visibility: manifest.visibility,
      retryable: false,
      warnings,
    });
  }
  let schedule = null;
  if (manifest.mode === "schedule") {
    schedule = await configureSchedule(page, manifest.schedule_at);
    if (!schedule.ok) return schedule;
  }
  const titleValue = await titleInput.evaluate((element) => element.value);
  const bodyValue = await bodyEditor.innerText();
  if (titleValue !== manifest.title || !normalizedEditorText(bodyValue).startsWith(normalizedEditorText(manifest.body))) {
    return result("FILL_FAILED", {
      error: "EDITOR_VALUE_MISMATCH",
      retryable: true,
      warnings,
    });
  }

  return result("FILLED", {
    job_id: manifest.job_id,
    account_profile: manifest.account_profile,
    content_fingerprint: manifest.content_fingerprint,
    image_count: manifest.images.length,
    title: manifest.title,
    tags: mergeTags(activitySelection.requiredTags, topicInsertion.inserted, topicInsertion.fallback),
    platform_topic_entities: topicInsertion.inserted,
    selected_activity: activitySelection.selected,
    activity_required_tags: activitySelection.requiredTags,
    schedule: schedule ? {
      requested_at: manifest.schedule_at,
      displayed_at: schedule.displayed_at,
      browser_time_zone: schedule.browser_time_zone,
    } : null,
    warnings,
    url: page.url(),
  });
}

async function findAction(page, labels) {
  const locators = [];
  for (const label of labels) {
    locators.push(page.getByRole("button", {name: label, exact: true}));
    locators.push(page.getByText(label, {exact: true}));
  }
  const semanticAction = await firstVisible(locators);
  if (semanticAction) return semanticAction;

  const host = page.locator(selectorConfig.selectors.publishHost).first();
  if (!await host.isVisible().catch(() => false)) return null;
  for (const label of labels) {
    const enabled = await host.evaluate((element, targetLabel) => {
      const button = [...(element._sr?.querySelectorAll("button") ?? [])]
        .find((candidate) => candidate.innerText.trim() === targetLabel);
      return Boolean(button && !button.disabled && button.getAttribute("aria-disabled") !== "true");
    }, label);
    if (!enabled) continue;
    return {
      click: () => host.evaluate((element, targetLabel) => {
        const button = [...(element._sr?.querySelectorAll("button") ?? [])]
          .find((candidate) => candidate.innerText.trim() === targetLabel);
        if (!button || button.disabled || button.getAttribute("aria-disabled") === "true") {
          throw new Error(`action_not_enabled:${targetLabel}`);
        }
        button.click();
      }, label),
    };
  }
  return null;
}

async function configureSchedule(page, scheduleAt) {
  if (!scheduleAt) {
    return result("SCHEDULE_FAILED", {
      error: "SCHEDULE_AT_REQUIRED",
      retryable: false,
    });
  }
  const toggle = page.locator(selectorConfig.selectors.scheduleToggle).first();
  const checkbox = page.locator(selectorConfig.selectors.scheduleCheckbox).first();
  if (await toggle.count() === 0 || await checkbox.count() === 0) {
    return result("ADAPTER_OUTDATED", {
      error: "SCHEDULE_CONTROL_NOT_FOUND",
      retryable: false,
    });
  }
  await toggle.scrollIntoViewIfNeeded();
  if (!await checkbox.isChecked()) {
    await toggle.click();
    await page.waitForTimeout(350);
  }
  const input = page.locator(selectorConfig.selectors.scheduleInput).first();
  const inputReady = await input.waitFor({state: "attached", timeout: 15000})
    .then(() => true)
    .catch(() => false);
  if (!inputReady) {
    return result("ADAPTER_OUTDATED", {
      error: "SCHEDULE_INPUT_NOT_FOUND",
      retryable: false,
    });
  }
  await input.scrollIntoViewIfNeeded();
  const browserTimeZone = await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  const localValue = formatDateTimeInZone(scheduleAt, browserTimeZone);
  await input.fill(localValue);
  await input.press("Enter").catch(() => null);
  await page.waitForTimeout(250);
  const displayedAt = await input.inputValue();
  if (displayedAt !== localValue) {
    return result("SCHEDULE_FAILED", {
      error: "SCHEDULE_VALUE_MISMATCH",
      requested_schedule_at: scheduleAt,
      expected_displayed_at: localValue,
      displayed_at: displayedAt,
      browser_time_zone: browserTimeZone,
      retryable: false,
    });
  }
  return result("SCHEDULE_CONFIGURED", {
    schedule_at: scheduleAt,
    displayed_at: displayedAt,
    browser_time_zone: browserTimeZone,
  });
}

async function verifyInManager(page, manifest, expectedKind) {
  await goto(page, selectorConfig.urls.noteManager);
  if (expectedKind === "schedule") {
    const browserTimeZone = await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
    const expectedLocalTime = formatDateTimeInZone(manifest.schedule_at, browserTimeZone);
    const expectedScheduleText = `定时发布 ${expectedLocalTime}`;
    let text = "";
    for (let attempt = 0; attempt < 12; attempt += 1) {
      text = await visibleText(page);
      if (text.includes(manifest.title) && text.includes(expectedScheduleText)) break;
      await page.waitForTimeout(300);
    }
    if (text.includes(manifest.title) && text.includes(expectedScheduleText)) {
      return result("SCHEDULED", {
        job_id: manifest.job_id,
        content_fingerprint: manifest.content_fingerprint,
        title: manifest.title,
        schedule_at: manifest.schedule_at,
        displayed_at: expectedLocalTime,
        url: page.url(),
      });
    }
    return result("UNKNOWN", {
      job_id: manifest.job_id,
      content_fingerprint: manifest.content_fingerprint,
      title: manifest.title,
      expected: "schedule",
      expected_displayed_at: expectedLocalTime,
      error: "MATCHING_SCHEDULE_NOT_FOUND",
      retryable: false,
      url: page.url(),
    });
  }
  const text = await visibleText(page);
  const titleFound = text.includes(manifest.title);
  if (!titleFound) {
    return result("UNKNOWN", {
      job_id: manifest.job_id,
      expected: expectedKind,
      error: "TITLE_NOT_FOUND_IN_NOTE_MANAGER",
      retryable: false,
      url: page.url(),
    });
  }
  if (expectedKind === "draft") {
    return result("DRAFT_SAVED", {
      job_id: manifest.job_id,
      content_fingerprint: manifest.content_fingerprint,
      title: manifest.title,
      url: page.url(),
    });
  }
  let status = expectedKind === "schedule" ? "SCHEDULED" : "SUBMITTED";
  if (text.includes("审核中")) status = "UNDER_REVIEW";
  if (text.includes("已发布") || text.includes("发布成功")) status = "PUBLISHED";
  if (text.includes("未通过") || text.includes("发布失败")) status = "REJECTED";
  return result(status, {
    job_id: manifest.job_id,
    content_fingerprint: manifest.content_fingerprint,
    title: manifest.title,
    url: page.url(),
  });
}

async function executeWrite(page, command, manifest) {
  const filled = await applyPost(page, manifest);
  if (!filled.ok) return filled;

  const labels = command === "draft"
    ? selectorConfig.signals.draftActions
    : command === "schedule"
      ? selectorConfig.signals.scheduleActions
      : selectorConfig.signals.publishActions;
  const action = await findAction(page, labels);
  if (!action) {
    return result("ADAPTER_OUTDATED", {
      error: command === "draft" ? "DRAFT_CONTROL_NOT_FOUND"
        : command === "schedule" ? "SCHEDULE_SUBMIT_CONTROL_NOT_FOUND"
          : "PUBLISH_CONTROL_NOT_FOUND",
      retryable: false,
      warnings: filled.warnings,
    });
  }

  await action.click();
  await page.waitForTimeout(1200);
  return verifyInManager(page, manifest, command);
}

async function keepOpenUntilInterrupted() {
  process.stderr.write("Browser remains open. Press Ctrl+C to close it.\n");
  await new Promise((resolvePromise) => {
    process.once("SIGINT", resolvePromise);
    process.once("SIGTERM", resolvePromise);
  });
}

async function main() {
  const {command, options} = parseArgs(process.argv.slice(2));
  if (!COMMANDS.has(command)) {
    process.stderr.write(`${usage()}\n`);
    process.exitCode = 2;
    return;
  }

  let manifest = null;
  if (command !== "preflight") {
    manifest = validateManifest(options.manifest);
  }
  if (command === "validate") {
    emit(result("VALID", {payload: manifest}));
    return;
  }
  if (WRITE_COMMANDS.has(command) && options.commit !== true) {
    emit(result("NEEDS_AUTHORIZATION", {
      error: "COMMIT_FLAG_REQUIRED",
      command,
      retryable: false,
    }));
    process.exitCode = 3;
    return;
  }

  let effectiveCommand = command;
  if (command === "dispatch") {
    const decision = options.decision;
    if (!new Set(["draft", "publish", "schedule"]).has(decision)) {
      emit(result("VALIDATION_FAILED", {
        error: "DISPATCH_DECISION_REQUIRED",
        allowed: ["draft", "publish", "schedule"],
        retryable: false,
      }));
      process.exitCode = 2;
      return;
    }
    if (manifest.mode !== decision) {
      emit(result("VALIDATION_FAILED", {
        error: "DECISION_MODE_MISMATCH",
        decision,
        manifest_mode: manifest.mode,
        retryable: false,
      }));
      process.exitCode = 2;
      return;
    }
    effectiveCommand = decision;
  }
  if (new Set(["draft", "publish", "schedule"]).has(effectiveCommand)
      && manifest.mode !== effectiveCommand) {
    emit(result("VALIDATION_FAILED", {
      error: "COMMAND_MODE_MISMATCH",
      command: effectiveCommand,
      manifest_mode: manifest.mode,
      retryable: false,
    }));
    process.exitCode = 2;
    return;
  }

  const cdpUrl = options["cdp-url"] || process.env.EPOST_XHS_CDP_URL;
  const profileDir = expandPath(options["profile-dir"] || process.env.EPOST_XHS_PROFILE_DIR);
  if (cdpUrl && profileDir) throw new Error("choose_cdp_url_or_profile_dir");
  if (!cdpUrl && !profileDir) throw new Error("cdp_url_or_profile_dir_required");

  let browser = null;
  let ownsContext = false;
  let context;
  if (cdpUrl) {
    browser = await chromium.connectOverCDP(cdpUrl, {
      isLocal: true,
      noDefaults: true,
      slowMo: Number(options["slow-mo"] || 25),
    });
    context = browser.contexts()[0];
    if (!context) throw new Error("cdp_default_context_not_found");
    attachedOverCdp = true;
  } else {
    context = await chromium.launchPersistentContext(profileDir, {
      channel: "chrome",
      headless: false,
      viewport: null,
      slowMo: Number(options["slow-mo"] || 25),
    });
    ownsContext = true;
  }
  const page = context.pages()[0] ?? await context.newPage();

  try {
    const session = await preflight(page);
    if (!session.ok || command === "preflight") {
      emit(session);
      if (!session.ok) process.exitCode = 4;
      if (options["keep-open"] === true) {
        await keepOpenUntilInterrupted();
      }
      return;
    }

    let output;
    if (effectiveCommand === "fill") {
      output = await applyPost(page, manifest);
    } else if (effectiveCommand === "verify") {
      output = await verifyInManager(page, manifest, manifest.mode);
    } else {
      output = await executeWrite(page, effectiveCommand, manifest);
    }

    if (options.screenshot) {
      await page.screenshot({path: expandPath(options.screenshot), fullPage: true});
      output.screenshot = expandPath(options.screenshot);
    }
    emit(output);
    if (!output.ok) process.exitCode = 4;

    if (options["keep-open"] === true) {
      await keepOpenUntilInterrupted();
    }
  } finally {
    if (ownsContext) await context.close();
  }
}

main().then(
  () => {
    if (attachedOverCdp) process.exit(process.exitCode ?? 0);
  },
  (error) => {
    emit(error.details ?? result("EXECUTION_FAILED", {
      error: error.message,
      retryable: false,
    }));
    if (attachedOverCdp) process.exit(1);
    process.exitCode = 1;
  },
);
