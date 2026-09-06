#!/usr/bin/env node

import assert from "node:assert/strict";
import {
  chooseCandidate,
  deriveContentTags,
  extractHashTags,
  extractRequiredTags,
  mergeTags,
  parsePopularity,
  topicEntityName,
} from "./xhs_matching.mjs";

assert.deepEqual(mergeTags(["#AI工具", "AI工具", " ", "摄影"]), ["AI工具", "摄影"]);
assert.deepEqual(extractHashTags("正文 #旅行攻略 #周末去哪儿"), ["旅行攻略", "周末去哪儿"]);
assert.deepEqual(extractRequiredTags("必带话题：#旅行摄影 #秋日记录"), ["旅行摄影", "秋日记录"]);
assert.equal(parsePopularity("已有 12.3万人参与"), 123000);
assert.equal(topicEntityName("#旅行摄影\n12.3万人参与"), "旅行摄影");

const tags = deriveContentTags("京郊周末旅行攻略", "胶片相机记录秋天旅行", [], 8);
assert(tags.includes("旅行攻略"));
assert(tags.includes("摄影"));

const choice = chooseCandidate(
  "京郊周末旅行 胶片摄影",
  [
    {text: "夏日美食季 80万人参与"},
    {text: "周末旅行影像征集 2.5万人参与 #旅行摄影"},
  ],
);
assert.equal(choice.candidate.text.includes("旅行影像"), true);
assert.equal(chooseCandidate("旅行", [{text: "美食活动"}]), null);

process.stdout.write(`${JSON.stringify({event: "matching_tests_passed", count: 8})}\n`);
