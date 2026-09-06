const assert = require('node:assert/strict');
const {test} = require('node:test');
const flow = require('../assets/workflow.js');
test('fresh session first selects a type, with no default', () => {
  const snapshot = {stage: 'AWAIT_KIND', contentKind: null, media: null, metadata: null};
  assert.equal(flow.defaultView(snapshot), 'kind');
  assert.equal(flow.canVisit(snapshot, 'metadata'), false);
  assert.equal(flow.canVisit(snapshot, 'dashboard'), false);
});
test('configured session can return to every material step', () => {
  const snapshot = {stage: 'CONFIGURING', contentKind: 'video', media: {kind: 'video'}, metadata: {}, sourceConfirmed: true};
  for (const step of ['kind', 'media', 'metadata', 'confirm', 'dashboard']) assert.equal(flow.canVisit(snapshot, step), true);
  assert.equal(flow.editable(snapshot), true);
});
test('changed materials must be confirmed before going back to distribution', () => {
  const snapshot = {stage: 'AWAIT_MATERIAL_CONFIRMATION', contentKind: 'video', media: {}, metadata: {}, sourceConfirmed: false};
  assert.equal(flow.canVisit(snapshot, 'confirm'), true);
  assert.equal(flow.canVisit(snapshot, 'dashboard'), false);
});
test('video and image routes match only corresponding adapters', () => {
  const caps = [{platform: 'youtube', mediaKinds: ['video']}, {platform: 'xiaohongshu', mediaKinds: ['image_post']}];
  assert.deepEqual(caps.filter(c => flow.compatible(c, 'video')).map(c => c.platform), ['youtube']);
  assert.deepEqual(caps.filter(c => flow.compatible(c, 'image_post')).map(c => c.platform), ['xiaohongshu']);
});
test('confirmed handoffs and executing/completed records are viewable but not editable', () => {
  for (const stage of ['AWAIT_AGENT', 'EXECUTING', 'PAUSED', 'FINISHED']) {
    const snapshot = {stage, contentKind: 'video', media: {}, metadata: {}};
    assert.equal(flow.canVisit(snapshot, 'metadata'), true);
    assert.equal(flow.canVisit(snapshot, 'dashboard'), true);
    assert.equal(flow.defaultView(snapshot), 'dashboard');
    assert.equal(flow.editable(snapshot), false);
    assert.equal(flow.committed(snapshot), true);
  }
});
test('waiting for an Agent is distinct from execution and provides a manual handoff', () => {
  const waiting = flow.executionPresentation({stage: 'AWAIT_AGENT', executionMode: 'agent'});
  const running = flow.executionPresentation({stage: 'EXECUTING', executionMode: 'agent'});
  assert.equal(waiting.showHandoff, true);
  assert.equal(running.showHandoff, false);
  assert.notEqual(waiting.title, running.title);
  assert.equal(flow.executionPresentation({stage: 'CONFIGURING'}), null);
});
test('receiver readiness only applies to the current session and asset revision', () => {
  const snapshot = {sessionId: 'batch-a', assetRevision: 7, stage: 'AWAIT_AGENT', agentReceiver: {status: 'ready', sessionId: 'batch-a', assetRevision: 7}};
  assert.equal(flow.receiver(snapshot).ready, true);
  assert.equal(flow.executionPresentation(snapshot).showHandoff, false);
  assert.equal(flow.receiver({...snapshot, assetRevision: 8}).ready, false);
  assert.equal(flow.receiver({...snapshot, sessionId: 'batch-b'}).ready, false);
  assert.equal(flow.receiver({...snapshot, agentReceiver: {...snapshot.agentReceiver, status: 'busy'}}).ready, false);
  assert.equal(flow.receiver({...snapshot, agentReceiver: {...snapshot.agentReceiver, status: 'offline'}}).ready, false);
  assert.notEqual(flow.executionPresentation(snapshot).title, flow.executionPresentation({...snapshot, stage: 'EXECUTING'}).title);
});
test('completed/paused task presentation takes precedence over an expired receiver', () => {
  for (const stage of ['FINISHED', 'PAUSED']) {
    const snapshot = dashboardFixture(stage);
    snapshot.agentReceiver = {status: 'offline', sessionId: snapshot.sessionId, assetRevision: snapshot.assetRevision, reason: 'expired'};
    const presentation = flow.receiver(snapshot);
    assert.equal(presentation.ready, false);
    assert.equal(presentation.status, stage === 'FINISHED' ? 'finished' : 'paused');
    assert.equal(presentation.tone, stage === 'FINISHED' ? 'info' : 'warning');
    assert.equal(presentation.message.includes('expired'), false);
    assert.notEqual(presentation.title, flow.receiver({...snapshot, stage: 'CONFIGURING'}).title);
  }
});
test('waiting/configuration receiver failure codes map to readable Chinese without raw codes', () => {
  for (const stage of ['CONFIGURING', 'AWAIT_AGENT']) {
    for (const reason of ['expired', 'not_connected', 'materials_changed', 'process_unverifiable', 'process_stopped', 'not_owned', 'unknown_internal_failure']) {
      const snapshot = dashboardFixture(stage);
      snapshot.agentReceiver = {status: 'offline', sessionId: snapshot.sessionId, assetRevision: snapshot.assetRevision, reason};
      const presentation = flow.receiver(snapshot);
      assert.equal(presentation.status, 'offline');
      assert.equal(presentation.ready, false);
      assert.equal(presentation.message.includes(reason), false);
      assert.equal(/[\u4e00-\u9fff]/.test(presentation.message), true);
    }
  }
});
test('handoff copies only verified locator fields, not arbitrary credentials or wrong sessions', () => {
  const snapshot = {stage: 'AWAIT_AGENT', executionMode: 'agent', sessionId: 'test-session', agentHandoff: {
    workspace: '/tmp/test workspace', sessionId: 'test-session', planHash: 'saved-plan-hash', token: 'DO_NOT_COPY', apiKey: 'DO_NOT_COPY',
  }};
  const text = flow.handoffText(snapshot);
  assert.deepEqual(JSON.parse(text.slice(text.indexOf('{'))), {
    workspace: '/tmp/test workspace', sessionId: 'test-session', planHash: 'saved-plan-hash',
  });
  assert.equal(text.includes('DO_NOT_COPY'), false);
  assert.equal(flow.handoffText({...snapshot, sessionId: 'different-session'}), '');
  assert.equal(flow.handoffText({...snapshot, stage: 'CONFIGURING'}), '');
  assert.equal(flow.handoffText({...snapshot, agentHandoff: {...snapshot.agentHandoff, planHash: ''}}), '');
});

// Exercise the actual dashboard script with a small DOM/clock test double. This
// checks bridge calls and UI locks, not browser layout or real platform behavior.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const dashboardSource = fs.readFileSync(path.join(__dirname, '../assets/dashboard.js'), 'utf8');
const dashboardHtml = fs.readFileSync(path.join(__dirname, '../assets/dashboard.html'), 'utf8');
function dashboardFixture(stage = 'AWAIT_AGENT') {
  return {
    stage, executionMode: 'agent', sessionId: 'isolated-session', assetRevision: 2, sourceConfirmed: true,
    contentKind: 'video', media: {kind: 'video', files: [{name: 'local-test.mp4', size: 4}]},
    metadata: {title: 'Test title', body: 'Test body', coverAbsent: true}, capabilities: [], results: [],
    agentHandoff: {status: 'waiting', workspace: '/tmp/isolated-test', sessionId: 'isolated-session', planHash: 'test-plan'},
    review: {rows: [{platform: 'youtube', actionLabel: '一键草稿', accountLabel: 'Test account', timeLabel: '不设置时间', draftScope: 'local'}], notices: []},
  };
}
async function loadDashboard(initialSnapshot) {
  const elements = [];
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag; this.children = []; this.dataset = {}; this.events = new Map(); this.value = '';
      this.files = []; this.checked = false; this.hidden = false; this.disabled = false; this.open = false; this._text = '';
      this.classList = {toggle() {}}; elements.push(this);
    }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map((child) => child.textContent || String(child)).join(''); }
    append(...children) { this.children.push(...children); }
    prepend(...children) { this.children.unshift(...children); }
    replaceChildren(...children) { this._text = ''; this.children = children; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    addEventListener(name, callback) { this.events.set(name, callback); }
    querySelector(selector) { return selector === 'button' ? this.children.find((child) => child.tagName === 'button') : null; }
    focus() {} select() {} scrollIntoView() {} showModal() { this.open = true; } close() { this.open = false; }
  }
  for (const match of dashboardHtml.matchAll(/<([a-z][a-z0-9-]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const element = new Element(match[1]); element.id = match[2];
  }
  const main = new Element('main');
  const stepButtons = ['media', 'metadata', 'confirm', 'dashboard'].map((step) => {
    const element = new Element('button'); element.dataset.goStep = step; return element;
  });
  const steps = stepButtons.map((button) => {
    const element = new Element('li'); element.dataset.step = button.dataset.goStep; element.append(button); return element;
  });
  const kinds = ['image_post', 'video'].map((kind) => { const element = new Element('input'); element.value = kind; return element; });
  const byId = (id) => elements.findLast((element) => element.id === id) || null;
  const document = {
    getElementById: byId,
    createElement: (tag) => new Element(tag),
    querySelector: (selector) => selector === 'main' ? main : selector === 'input[name="content-kind"]:checked' ? kinds.find((input) => input.checked) || null : null,
    querySelectorAll: (selector) => selector === '[data-go-step]' ? stepButtons : selector === '[data-step]' ? steps : selector === 'input[name="content-kind"]' ? kinds : selector === '[data-edit-control]' ? elements.filter((element) => element.dataset.editControl) : [],
  };
  let snapshot = initialSnapshot; let timerId = 0; let nextRead = null;
  const timers = new Map(); const calls = [];
  const host = {protocolVersion: '1', snapshot: async () => { calls.push('snapshot'); const pending = nextRead; nextRead = null; return pending ? await pending : structuredClone(snapshot); }};
  for (const method of ['selectKind', 'storeMedia', 'storeMetadata', 'confirmMaterials', 'preparePlan', 'confirmPlan']) {
    host[method] = async () => { calls.push(method); throw new Error(`Unexpected mutation: ${method}`); };
  }
  const window = {
    OneClickPublishWorkflow: flow, OneClickPublishHost: host, location: {href: 'http://127.0.0.1:1/'},
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(dashboardSource, {document, window, navigator: {}, URL, Intl}, {filename: 'dashboard.js'});
  await new Promise(setImmediate);
  return {
    byId, calls, timers, stepButtons,
    byFocusKey: (key) => elements.findLast((element) => element.dataset.focusKey === key),
    setSnapshot(value) { snapshot = value; },
    setNextRead(promise) { nextRead = promise; },
    setMutationResult(method, result) { host[method] = async () => { calls.push(method); return structuredClone(result); }; },
    async click(id) { await byId(id).events.get('click')?.(); },
    async tick() {
      assert.equal(timers.size, 1, 'exactly one read-only progress timer');
      const [id, callback] = timers.entries().next().value; timers.delete(id); await callback();
    },
  };
}
test('reload of a confirmed handoff locks editing, shows saved plan, and never dispatches', async () => {
  const page = await loadDashboard(dashboardFixture());
  assert.equal(page.byId('agent-handoff').hidden, false);
  assert.equal(page.byId('configuration-controls').hidden, true);
  assert.equal(page.byId('kind-fields').disabled, true);
  assert.equal(page.byId('metadata-fields').disabled, true);
  assert.equal(page.byId('prepare-plan-button').disabled, true);
  assert.equal(page.byId('confirmed-plan-rows').children.length, 1);
  assert.equal(page.byId('results-heading').textContent, flow.executionPresentation(dashboardFixture()).title);
  await page.click('refresh-button');
  await page.click('confirm-yes-button');
  await page.click('store-metadata-button');
  await page.stepButtons.find((button) => button.dataset.goStep === 'metadata').events.get('click')();
  assert.equal(page.byId('metadata-section').hidden, false);
  assert.equal(page.byId('metadata-fields').disabled, true);
  assert.deepEqual(page.calls, ['snapshot', 'snapshot']);
});
test('automatic progress reads update waiting/running/paused/completed without execution calls', async () => {
  const page = await loadDashboard(dashboardFixture());
  page.byId('status-message').textContent = '发布指令已保存，等待 Agent 接收';
  page.byId('status-message').hidden = false;
  for (const stage of ['EXECUTING', 'PAUSED', 'FINISHED']) {
    const snapshot = dashboardFixture(stage);
    if (stage === 'PAUSED') snapshot.results = [{platform: 'youtube', status: 'blocked', message: 'Fixture missing credential'}];
    page.setSnapshot(snapshot); await page.tick();
    assert.equal(page.byId('results-heading').textContent, flow.executionPresentation(snapshot).title);
    assert.equal(page.byId('status-message').hidden, true);
    if (stage === 'PAUSED') assert.equal(page.byId('result-list').children.length, 1);
  }
  assert.equal(page.timers.size, 0, 'no more reads once finished');
  assert.deepEqual(page.calls, ['snapshot', 'snapshot', 'snapshot', 'snapshot']);
});
test('chat collection is the default, with the original web upload available as fallback', async () => {
  const fixture = {...dashboardFixture('AWAIT_KIND'), contentKind: null, media: null, metadata: null, sourceConfirmed: false};
  const page = await loadDashboard(fixture);
  assert.equal(page.timers.size, 0);
  assert.equal(page.byId('chat-materials-section').hidden, false);
  assert.equal(page.byId('kind-section').hidden, true);
  await page.click('use-web-upload-button');
  assert.equal(page.byId('kind-section').hidden, false);
  assert.equal(page.byId('chat-materials-section').hidden, true);
  await page.click('use-chat-upload-button');
  assert.equal(page.byId('chat-materials-section').hidden, false);
  assert.deepEqual(page.calls, ['snapshot']);
});
test('configuration polling updates only receiver status and preserves every unsaved field/control', async () => {
  const fixture = dashboardFixture('CONFIGURING');
  fixture.capabilities = [{platform: 'youtube', available: true, mediaKinds: ['video'], modes: ['draft', 'publish', 'schedule'], accounts: [{id: 'test', label: 'Test'}], requiredPostSettings: []}];
  const page = await loadDashboard(fixture);
  await page.byFocusKey('youtube-quick-action-publish').events.get('change')();
  await page.byFocusKey('youtube-timing-scheduled').events.get('change')();
  page.byId('youtube-region').value = 'us'; await page.byId('youtube-region').events.get('change')();
  page.byId('youtube-timezone').value = 'America/New_York'; await page.byId('youtube-timezone').events.get('change')();
  page.byId('youtube-date').value = '2026-10-05'; await page.byId('youtube-date').events.get('change')();
  page.byId('youtube-time').value = '16:37'; await page.byId('youtube-time').events.get('input')();
  page.byId('title-text').value = 'Unsaved title must survive';
  const title = page.byId('title-text'); const date = page.byId('youtube-date'); const time = page.byId('youtube-time');
  const checkbox = page.byFocusKey('youtube-selected'); const publish = page.byFocusKey('youtube-action-publish');
  const region = page.byId('youtube-region'); const timezone = page.byId('youtube-timezone');
  fixture.agentReceiver = {status: 'ready', sessionId: fixture.sessionId, assetRevision: fixture.assetRevision};
  page.setSnapshot(fixture); await page.tick();
  assert.equal(page.byId('agent-receiver-status').textContent.startsWith(flow.receiver(fixture).title), true);
  for (const [id, original] of [['title-text', title], ['youtube-date', date], ['youtube-time', time], ['youtube-region', region], ['youtube-timezone', timezone]]) assert.equal(page.byId(id), original, `must not replace ${id}`);
  assert.equal(title.value, 'Unsaved title must survive'); assert.equal(date.value, '2026-10-05'); assert.equal(time.value, '16:37');
  assert.equal(region.value, 'us'); assert.equal(timezone.value, 'America/New_York');
  assert.equal(page.byFocusKey('youtube-selected'), checkbox); assert.equal(checkbox.checked, true);
  assert.equal(page.byFocusKey('youtube-action-publish'), publish); assert.equal(publish.checked, true);
  assert.deepEqual(page.calls, ['snapshot', 'snapshot']);
});
test('stale receiver/version cannot authorize or overwrite a configuration being edited', async () => {
  const fixture = dashboardFixture('CONFIGURING'); const page = await loadDashboard(fixture);
  const title = page.byId('title-text'); title.value = 'Keep unsaved local text';
  page.setSnapshot({...fixture, assetRevision: 3, agentReceiver: {status: 'ready', sessionId: fixture.sessionId, assetRevision: 3}});
  await page.tick();
  assert.equal(title.value, 'Keep unsaved local text');
  assert.equal(page.byId('prepare-plan-button').disabled, true);
  assert.equal(page.byId('confirm-yes-button').disabled, true);
  assert.equal(page.byId('agent-receiver-status').className, 'message warning');
  assert.deepEqual(page.calls, ['snapshot', 'snapshot']);
});
test('final confirmation receiver refresh does not change the displayed review or create approval', async () => {
  const fixture = dashboardFixture('AWAIT_PLAN_CONFIRMATION');
  fixture.pendingReview = {...fixture.review, sessionId: fixture.sessionId, planId: 'review-id', planHash: 'review-hash', question: 'Fixture confirmation?'};
  const page = await loadDashboard(fixture); const reviewRow = page.byId('confirmation-rows').children[0];
  fixture.agentReceiver = {status: 'ready', sessionId: fixture.sessionId, assetRevision: fixture.assetRevision};
  page.setSnapshot(fixture); await page.tick();
  assert.equal(page.byId('confirmation-rows').children[0], reviewRow);
  assert.equal(page.byId('confirmation-dialog').open, true);
  assert.equal(page.byId('confirmation-receiver-status').textContent.startsWith(flow.receiver(fixture).title), true);
  assert.equal(page.byId('confirm-yes-button').disabled, false);
  assert.deepEqual(page.calls, ['snapshot', 'snapshot']);
});
test('a stale receiver read cannot roll back a concurrently confirmed task', async () => {
  const fixture = dashboardFixture('AWAIT_PLAN_CONFIRMATION');
  fixture.pendingReview = {...fixture.review, sessionId: fixture.sessionId, planId: 'review-id', planHash: 'review-hash', question: 'Fixture confirmation?'};
  const page = await loadDashboard(fixture); const confirmed = dashboardFixture('AWAIT_AGENT');
  let resolveRead;
  page.setNextRead(new Promise((resolve) => { resolveRead = resolve; }));
  const pendingTick = page.tick();
  page.setMutationResult('confirmPlan', confirmed);
  await page.click('confirm-yes-button');
  assert.equal(page.byId('configuration-controls').hidden, true);
  resolveRead(structuredClone(fixture)); await pendingTick;
  assert.equal(page.byId('results-heading').textContent, flow.executionPresentation(confirmed).title);
  assert.equal(page.byId('confirmation-dialog').open, false);
  assert.equal(page.byId('configuration-controls').hidden, true);
  assert.equal(page.byId('prepare-plan-button').disabled, true);
  assert.deepEqual(page.calls, ['snapshot', 'snapshot', 'confirmPlan']);
});
test('finished page treats receiver expiry as normal and never suggests another reception', async () => {
  const fixture = dashboardFixture('FINISHED');
  fixture.agentReceiver = {status: 'offline', sessionId: fixture.sessionId, assetRevision: fixture.assetRevision, reason: 'expired'};
  const page = await loadDashboard(fixture);
  assert.equal(page.byId('agent-receiver-status').className, 'message info');
  assert.equal(page.byId('agent-receiver-status').textContent, `${flow.receiver(fixture).title}。${flow.receiver(fixture).message}`);
  assert.equal(page.byId('agent-handoff').hidden, true);
  assert.equal(page.timers.size, 0);
  assert.deepEqual(page.calls, ['snapshot']);
});
