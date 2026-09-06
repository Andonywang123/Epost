const CJK_SEQUENCE = /[\u3400-\u4dbf\u4e00-\u9fff]{2,16}/gu;
const HASH_TAG = /[#＃]([\p{L}\p{N}_\-]{2,30})/gu;

const GENERIC_WORDS = new Set([
  "活动", "话题", "热门", "推荐", "参与", "参加", "发布", "笔记", "更多", "搜索",
  "activity", "topic", "popular", "recommended", "publish", "more", "search",
]);

const DOMAIN_TAGS = [
  {terms: ["ai", "chatgpt", "人工智能", "大模型", "提示词"], tags: ["AI工具", "人工智能", "效率工具"]},
  {terms: ["摄影", "相机", "拍照", "胶片", "镜头"], tags: ["摄影", "摄影技巧", "影像记录"]},
  {terms: ["旅行", "旅游", "攻略", "酒店", "景点"], tags: ["旅行攻略", "旅行记录", "周末去哪儿"]},
  {terms: ["美食", "菜谱", "烘焙", "料理", "餐厅"], tags: ["美食分享", "菜谱", "好好吃饭"]},
  {terms: ["穿搭", "护肤", "彩妆", "口红", "衣服"], tags: ["穿搭分享", "变美思路", "日常穿搭"]},
  {terms: ["职场", "办公", "工作", "求职", "简历"], tags: ["职场干货", "工作效率", "成长记录"]},
  {terms: ["学习", "读书", "考试", "英语", "笔记"], tags: ["学习方法", "读书笔记", "自我提升"]},
  {terms: ["家居", "装修", "收纳", "房间", "桌面"], tags: ["家居生活", "收纳整理", "居家好物"]},
  {terms: ["健身", "跑步", "瑜伽", "训练", "运动"], tags: ["健身打卡", "运动日常", "健康生活"]},
];

export function cleanTag(value) {
  return String(value ?? "")
    .trim()
    .replace(/^[#＃]+/u, "")
    .replace(/[\s#＃]+/gu, " ")
    .replace(/[^㐀-䶿一-鿿\p{L}\p{N}_\- ]/gu, "")
    .trim()
    .slice(0, 30);
}

export function mergeTags(...groups) {
  const result = [];
  const seen = new Set();
  for (const value of groups.flat()) {
    const tag = cleanTag(value);
    const key = tag.toLocaleLowerCase();
    if (!tag || seen.has(key)) continue;
    seen.add(key);
    result.push(tag);
  }
  return result;
}

export function extractHashTags(text) {
  return mergeTags([...(String(text ?? "").matchAll(HASH_TAG))].map((match) => match[1]));
}

export function extractRequiredTags(text) {
  const value = String(text ?? "");
  const tags = extractHashTags(value);
  for (const match of value.matchAll(/(?:必带|需带|活动)(?:话题|标签)\s*[:：]?\s*([^\n；;]{2,80})/gu)) {
    tags.push(...match[1].split(/[\s,，、/|]+/u));
  }
  return mergeTags(tags).slice(0, 10);
}

export function topicEntityName(text) {
  const hashtags = extractHashTags(text);
  if (hashtags.length) return hashtags[0];
  const firstLine = String(text ?? "").split("\n").map((part) => part.trim()).find(Boolean) ?? "";
  return cleanTag(firstLine.replace(
    /\s*\d+(?:\.\d+)?\s*(?:亿|万|[kKmM])?\s*(?:参与|浏览|播放|热度|笔记|人气|次).*$/u,
    "",
  ));
}

function comparable(value) {
  return String(value ?? "").toLocaleLowerCase().replace(/[^\u3400-\u4dbf\u4e00-\u9fff\p{L}\p{N}]+/gu, "");
}

function units(value) {
  const text = String(value ?? "").toLocaleLowerCase();
  const result = new Set();
  for (const word of text.match(/[a-z0-9][a-z0-9_\-]{1,30}/g) ?? []) {
    if (!GENERIC_WORDS.has(word)) result.add(word);
  }
  for (const sequence of text.match(CJK_SEQUENCE) ?? []) {
    if (!GENERIC_WORDS.has(sequence)) result.add(sequence);
    for (const width of [2, 3, 4]) {
      for (let index = 0; index <= sequence.length - width; index += 1) {
        result.add(sequence.slice(index, index + width));
      }
    }
  }
  for (const tag of extractHashTags(text)) result.add(tag.toLocaleLowerCase());
  return result;
}

export function parsePopularity(text) {
  const match = String(text ?? "").match(/(\d+(?:\.\d+)?)\s*(亿|万|[kKmM])?(?:\s*(?:参与|浏览|播放|热度|笔记|人气|次))?/u);
  if (!match) return 0;
  const multiplier = match[2] === "亿" ? 100_000_000
    : match[2] === "万" ? 10_000
      : /m/i.test(match[2] ?? "") ? 1_000_000
        : /k/i.test(match[2] ?? "") ? 1_000 : 1;
  return Number(match[1]) * multiplier;
}

export function relevanceScore(context, candidateText) {
  const candidate = comparable(candidateText);
  if (!candidate) return 0;
  const contextText = comparable(context);
  let score = 0;
  const contextUnits = units(context);
  const candidateUnits = units(candidateText);
  for (const unit of contextUnits) {
    const normalized = comparable(unit);
    if (normalized.length >= 2 && candidate.includes(normalized)) score += normalized.length >= 4 ? 5 : 2;
  }
  for (const unit of candidateUnits) {
    const normalized = comparable(unit);
    if (normalized.length >= 2 && contextText.includes(normalized)) score += normalized.length >= 4 ? 5 : 2;
  }
  return score;
}

export function chooseCandidate(context, candidates, options = {}) {
  const exactName = cleanTag(options.exactName ?? "");
  const exactKey = comparable(exactName);
  const ranked = candidates.map((candidate, index) => {
    const text = String(candidate.text ?? candidate);
    const candidateKey = comparable(text);
    const exact = exactKey && (candidateKey === exactKey || candidateKey.includes(exactKey));
    const semantic = relevanceScore(context, text);
    const popularity = parsePopularity(text);
    const popularityBonus = popularity > 0 ? Math.min(4, Math.log10(popularity + 1)) : 0;
    return {candidate, index, exact, semantic, popularity, score: semantic + popularityBonus + (exact ? 100 : 0)};
  }).sort((left, right) => right.score - left.score || right.popularity - left.popularity || left.index - right.index);
  const best = ranked[0];
  if (!best) return null;
  if (exactKey && !best.exact) return null;
  const minimum = options.minimumScore ?? 4;
  if (!exactKey && best.semantic < minimum) return null;
  return best;
}

export function deriveContentTags(title, body, seeds = [], limit = 8) {
  const combined = `${title}\n${body}`;
  const candidates = mergeTags(seeds, extractHashTags(combined));
  const normalized = combined.toLocaleLowerCase();
  for (const mapping of DOMAIN_TAGS) {
    if (mapping.terms.some((term) => normalized.includes(term))) candidates.push(...mapping.tags);
  }
  const titleText = cleanTag(title);
  if (titleText.length >= 2 && titleText.length <= 12) candidates.unshift(titleText);
  for (const phrase of combined.split(/[\s,，。.!！?？:：;；、|/\\()（）【】\[\]]+/u)) {
    const tag = cleanTag(phrase);
    if (tag.length >= 2 && tag.length <= 10) candidates.push(tag);
  }
  return mergeTags(candidates).slice(0, Math.max(1, Math.min(10, Number(limit) || 8)));
}

export function activityContext(manifest) {
  return [manifest.title, manifest.body, ...(manifest.tags ?? [])].filter(Boolean).join(" ");
}
