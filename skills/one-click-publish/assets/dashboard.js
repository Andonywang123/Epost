/*
 * Embeddable / portable dashboard, protocol version 1.
 * No platform SDK, credentials or publishing logic live here. The progress timer
 * only reads snapshots; it never confirms, starts, cancels or retries a task.
 * portable-host.js may provide a same-origin local-service bridge. An embedding
 * application may instead inject window.OneClickPublishHost before this script.
 * A JavaScript object is not an authentication boundary: the host must authenticate
 * every request, bind it to the current user/session, validate all input and enforce
 * content hashes, exact authorization, time zones and idempotency server-side.
 * snapshot() must only read cached local state; in the two upload stages the only
 * mutating methods invoked here are storeMedia() and storeMetadata(). TXT reads are
 * literal File.text() reads for field mapping, never semantic analysis or rewriting.
 */
(() => {
  'use strict';

  const PROTOCOL = '1';
  const METHODS = ['snapshot', 'selectKind', 'storeMedia', 'storeMetadata', 'confirmMaterials', 'preparePlan', 'confirmPlan'];
  const flow = window.OneClickPublishWorkflow;
  const STAGES = new Set(['AWAIT_KIND', 'AWAIT_MEDIA', 'AWAIT_METADATA', 'AWAIT_MATERIAL_CONFIRMATION', 'CONFIGURING', 'AWAIT_PLAN_CONFIRMATION', 'AWAIT_AGENT', 'EXECUTING', 'FINISHED', 'PAUSED']);
  const DASHBOARD_STAGES = new Set(['CONFIGURING', 'AWAIT_PLAN_CONFIRMATION', 'AWAIT_AGENT', 'EXECUTING', 'FINISHED', 'PAUSED']);
  const PLATFORMS = [
    { id: 'xiaohongshu', label: '小红书', foreign: false },
    { id: 'douyin', label: '抖音', foreign: false },
    { id: 'wechat_channels', label: '微信视频号', foreign: false },
    { id: 'bilibili', label: 'B站', foreign: false },
    { id: 'youtube', label: 'YouTube', foreign: true },
    { id: 'instagram', label: 'ins', foreign: true },
    { id: 'tiktok', label: 'TikTok', foreign: true },
    { id: 'x', label: 'X', foreign: true }
  ];
  const REGIONS = [
    ['china', '中国区'], ['us', '美区'], ['uk_eu', '英欧区'],
    ['australia', '澳区'], ['local', '本地时间']
  ];
  const ZONES = {
    us: [['America/Los_Angeles', '洛杉矶'], ['America/New_York', '纽约'], ['America/Chicago', '芝加哥']],
    uk_eu: [['Europe/London', '伦敦'], ['Europe/Paris', '巴黎'], ['Europe/Berlin', '柏林']],
    australia: [['Australia/Sydney', '悉尼'], ['Australia/Perth', '珀斯'], ['Australia/Brisbane', '布里斯班']]
  };
  const STAGE_LABELS = {
    AWAIT_KIND: '请选择图文或视频',
    AWAIT_MEDIA: '等待第一批素材', AWAIT_METADATA: '等待封面与文案',
    AWAIT_MATERIAL_CONFIRMATION: '等待确认素材', CONFIGURING: '正在配置',
    AWAIT_PLAN_CONFIRMATION: '等待最终确认', AWAIT_AGENT: '等待 Agent 接手', EXECUTING: '正在逐一执行',
    FINISHED: '执行流程已结束', PAUSED: '执行已暂停'
  };
  const $ = (id) => document.getElementById(id);
  const state = {
    host: null, connected: false, snapshot: null, busy: false, view: null,
    rows: new Map(), review: null, finalAttempted: false,
    titleSource: null, bodySource: null, titleRaw: null, bodyRaw: null, localZone: null,
    progressTimer: null, polling: false, progressReadFailed: false,
    webUpload: false, remoteChanged: false, snapshotGeneration: 0
  };
  try { state.localZone = Intl.DateTimeFormat().resolvedOptions().timeZone || null; } catch { /* Require explicit selection if detection fails. */ }

  function node(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  }
  function plain(value) {
    if (value === null || value === undefined) return '';
    return typeof value === 'string' ? value : JSON.stringify(value);
  }
  function setVisible(id, visible) { $(id).hidden = !visible; }
  function showError(message, focus = false) {
    $('error-message').textContent = plain(message);
    $('error-message').hidden = !message;
    if (focus && message) $('error-message').focus();
  }
  function announce(message) {
    $('status-message').textContent = plain(message);
    $('status-message').hidden = !message;
  }
  function errorText(error) {
    return error && typeof error.message === 'string' ? error.message : plain(error) || '未收到明确结果，请刷新进度后核对。';
  }
  function detectHost() {
    const bridge = window.OneClickPublishHost;
    if (!bridge || bridge.protocolVersion !== PROTOCOL || !METHODS.every((method) => typeof bridge[method] === 'function')) return null;
    return bridge;
  }
  function currentStage() { return state.snapshot ? state.snapshot.stage : null; }
  function currentView() { return state.view || flow.defaultView(state.snapshot); }
  function canEdit() { return !!state.host && state.connected && !state.remoteChanged && flow.editable(state.snapshot) && !state.review && !state.finalAttempted; }
  function canConfigure() { return canEdit() && currentStage() === 'CONFIGURING' && currentView() === 'dashboard' && !materialsDirty(); }
  function canEditMedia() { return canEdit() && currentView() === 'media' && !!flow.kind(state.snapshot); }
  function canEditMetadata() { return canEdit() && currentView() === 'metadata' && !!state.snapshot?.media; }
  function mediaDirty() { return ($('media-files').files?.length || 0) > 0; }
  function metadataValues() {
    return {title: state.titleSource ? state.titleRaw : $('title-text').value,
      body: state.bodySource ? state.bodyRaw : $('body-text').value,
      coverAbsent: $('cover-absent').checked, cover: $('cover-file').files?.[0] || null,
      keepCoverRevision: $('keep-cover').checked && state.snapshot?.metadata?.cover ? state.snapshot.assetRevision : null};
  }
  function metadataDirty() {
    const old = state.snapshot?.metadata; const value = metadataValues();
    if (!old) return !!(value.title || value.body || value.cover || value.coverAbsent || state.titleSource || state.bodySource);
    // A textarea may normalize line endings without the user editing it.
    const normalized = (text) => (text || '').replace(/\r\n?/g, '\n');
    return !!value.cover || normalized(value.title) !== normalized(old.title) || normalized(value.body) !== normalized(old.body)
      || value.coverAbsent !== old.coverAbsent || (!!old.cover && value.keepCoverRevision === null);
  }
  function materialsDirty() { return mediaDirty() || metadataDirty(); }
  function navigate(step) {
    if (state.busy || !state.connected || state.review || state.finalAttempted) return;
    if (step === 'media' && !flow.kind(state.snapshot)) step = 'kind';
    if (!flow.canVisit(state.snapshot, step)) { showError('请先完成前面的素材上传与确认，再进入此步骤。', true); return; }
    if (['confirm', 'dashboard'].includes(step) && materialsDirty()) {
      showError('有尚未保存的素材修改。请先在第 1、2 步保存，或明确放弃本步修改，再查看确认与发布。', true); return;
    }
    showError(''); state.view = step; render();
    $(step === 'kind' ? 'kind-heading' : step === 'media' ? 'media-heading' : step === 'metadata' ? 'metadata-heading' : step === 'confirm' ? 'materials-heading' : 'dashboard-heading').scrollIntoView({block: 'start'});
  }
  function capability(platform) {
    return state.snapshot?.capabilities.find((entry) => entry.platform === platform) || {
      platform, available: false, mediaKinds: [], modes: [], accounts: [], draftScope: null,
      reason: '尚未接入此平台发布包。'
    };
  }
  function accountsFor(cap) { return Array.isArray(cap.accounts) ? cap.accounts : []; }
  const POST_FIELDS = {
    visibility: {label: '小红书发布可见范围', options: [['公开', '公开（草稿仍只保存，不公开）']]},
    privacy: {label: 'YouTube 视频公开范围', options: [['private', '私密'], ['unlisted', '不公开列出'], ['public', '公开']]},
    made_for_kids: {label: '这个视频是否面向儿童？', options: [[true, '是，面向儿童'], [false, '否，不面向儿童']]},
    contains_synthetic_media: {label: '是否包含需要披露的逼真合成或修改内容？', options: [[true, '是'], [false, '否']]},
    notify_subscribers: {label: '发布时是否通知订阅者？', options: [[true, '是'], [false, '否']]}
  };
  function postFields(cap) { return Array.isArray(cap.requiredPostSettings) ? cap.requiredPostSettings : []; }
  function modesFor(cap) { return Array.isArray(cap.modes) ? cap.modes : []; }
  function mediaCompatible(cap) { return flow.compatible(cap, flow.kind(state.snapshot)); }
  function labelFor(platform) { return PLATFORMS.find((item) => item.id === platform)?.label || platform; }
  function ensureRows(reset = false) {
    if (reset) state.rows.clear();
    PLATFORMS.forEach((platform) => {
      if (!state.rows.has(platform.id)) {
        state.rows.set(platform.id, {
          platform: platform.id, selected: false, action: null, timingKind: null,
          region: platform.foreign ? null : 'china',
          timezone: platform.foreign ? null : 'Asia/Shanghai',
          date: '', minute: null, fold: null, accountProfile: '', acceptLocalDraft: false,
          accountConfirmed: false, postSettings: {}
        });
      }
      const row = state.rows.get(platform.id);
      const accounts = accountsFor(capability(platform.id));
      if (!row.accountProfile && accounts.length === 1) row.accountProfile = accounts[0].id;
    });
  }
  function validateReview(review) {
    if (!review || review.sessionId !== state.snapshot?.sessionId || typeof review.planId !== 'string' || !review.planId || typeof review.planHash !== 'string' || !review.planHash || typeof review.question !== 'string' || !Array.isArray(review.rows) || !Array.isArray(review.notices)) {
      throw new Error('未收到完整的确认摘要，本页不会执行，请刷新进度。');
    }
    const platforms = review.rows.map((row) => row.platform);
    if (!platforms.length || new Set(platforms).size !== platforms.length || platforms.some((platform) => !PLATFORMS.some((entry) => entry.id === platform))) {
      throw new Error('确认摘要中的平台无效或重复；本页不会执行。');
    }
    if (review.rows.some((row) => ['actionLabel', 'timeLabel', 'accountLabel'].some((key) => typeof row[key] !== 'string' || !row[key].trim()) || ![null, 'platform', 'local'].includes(row.draftScope))) {
      throw new Error('确认摘要缺少明确的操作、时间、账号或草稿位置；本页不会执行。');
    }
    return review;
  }
  function applySnapshot(snapshot, resetForms = false) {
    if (!snapshot || typeof snapshot.sessionId !== 'string' || !snapshot.sessionId || !STAGES.has(snapshot.stage) || snapshot.assetRevision === undefined || !Array.isArray(snapshot.capabilities) || !Array.isArray(snapshot.results)) {
      throw new Error('进度信息不完整或程序版本不匹配，暂时不能继续储存或发布。');
    }
    const changedSession = state.snapshot?.sessionId !== snapshot.sessionId;
    const changedKind = flow.kind(state.snapshot) !== flow.kind(snapshot);
    if (changedSession) {
      state.review = null;
      state.finalAttempted = false;
      state.view = null;
    }
    state.snapshot = snapshot;
    state.snapshotGeneration += 1;
    state.connected = true;
    state.remoteChanged = false;
    ensureRows(changedSession || changedKind);
    if (changedKind && !changedSession) { state.view = null; populateMedia(); }
    if (snapshot.stage !== 'AWAIT_PLAN_CONFIRMATION') {
      state.review = null;
      state.finalAttempted = false;
    } else if (snapshot.pendingReview) {
      state.review = validateReview(snapshot.pendingReview);
    }
    if (resetForms || changedSession) { populateMetadata(); populateMedia(); }
    if (flow.committed(snapshot) && !flow.canVisit(snapshot, currentView())) state.view = 'dashboard';
    render();
  }
  function setBusy(busy, message = '') {
    state.busy = busy;
    document.querySelector('main').setAttribute('aria-busy', String(busy));
    $('confirmation-dialog').setAttribute('aria-busy', String(busy));
    if (message) announce(message);
    updateLocks();
  }
  async function refreshSnapshot() {
    if (state.busy || state.polling) return;
    state.host = detectHost();
    if (!state.host) {
      state.connected = false;
      render();
      showError('看板未连接，请启动一键发布程序后刷新。当前不能储存或发布。');
      return;
    }
    setBusy(true, '正在更新进度，不触发发布。');
    showError('');
    try {
      const snapshot = await state.host.snapshot();
      const changedAssets = state.snapshot && (snapshot.sessionId !== state.snapshot.sessionId || snapshot.assetRevision !== state.snapshot.assetRevision);
      if (changedAssets && materialsDirty()) {
        state.remoteChanged = true;
        showError('Agent 中的素材已更新。本页未保存的输入仍保留；请先明确放弃本步修改，再刷新核对新素材。当前不会提交过期方案。', true);
        return;
      }
      if (changedAssets) state.view = null;
      applySnapshot(snapshot, !!changedAssets);
      announce('');
    } catch (error) {
      // Retain known state for inspection; never invent a successful transition.
      state.connected = false;
      showError(errorText(error));
    } finally { setBusy(false); renderConnection(); }
  }
  function shouldReadProgress() {
    return !!state.snapshot && currentStage() !== 'FINISHED'
      && (flow.committed(state.snapshot) || ['CONFIGURING', 'AWAIT_PLAN_CONFIRMATION'].includes(currentStage()) || !!state.review);
  }
  function scheduleProgressRefresh() {
    if (!shouldReadProgress()) {
      if (state.progressTimer !== null) window.clearTimeout(state.progressTimer);
      state.progressTimer = null;
      return;
    }
    if (state.progressTimer !== null || state.polling) return;
    state.progressTimer = window.setTimeout(readProgress, 5000);
  }
  async function readProgress() {
    state.progressTimer = null;
    if (!shouldReadProgress()) return;
    if (state.busy || state.finalAttempted || !state.host) {
      scheduleProgressRefresh(); return;
    }
    state.polling = true;
    const generation = state.snapshotGeneration;
    try {
      // This is the only bridge method invoked by the timer. It reads the
      // existing session; neither navigation nor page lifetime owns execution.
      const snapshot = await state.host.snapshot();
      // A user may confirm or save while this read is in flight. A stale GET
      // must never roll a confirmed/updated batch back into an editable stage.
      if (generation !== state.snapshotGeneration || state.busy || state.finalAttempted) return;
      if (!snapshot || snapshot.sessionId !== state.snapshot.sessionId) throw new Error('返回的任务与当前批次不一致，请手动核对。');
      if (flow.committed(state.snapshot)) {
        applySnapshot(snapshot);
        if (snapshot.stage !== 'AWAIT_AGENT') announce('');
      } else {
        // Configuration polling only reads receiver status. Do not apply the
        // snapshot, rebuild controls, change field values, or replace state.rows.
        const sameStage = snapshot.stage === currentStage()
          || state.review && snapshot.stage === 'AWAIT_PLAN_CONFIRMATION' && snapshot.pendingReview?.planHash === state.review.planHash;
        state.snapshot.agentReceiver = snapshot.agentReceiver;
        state.connected = true;
        if (snapshot.assetRevision !== state.snapshot.assetRevision || !sameStage) {
          state.remoteChanged = true;
          showError('另一入口更新了素材或任务记录。本页输入与平台选择仍保留，请刷新后核对；过期方案不会提交。');
          updateLocks();
        }
        renderReceiver(); renderConnection();
      }
      if (state.progressReadFailed && !state.remoteChanged) { showError(''); state.progressReadFailed = false; }
    } catch (error) {
      state.connected = false;
      state.progressReadFailed = true;
      renderConnection();
      renderReceiver();
      showError(`暂时无法读取最新进度：${errorText(error)}。这不表示 Agent 已停止；请到 Agent 应用核对，不要重复提交。`);
    } finally { state.polling = false; scheduleProgressRefresh(); }
  }
  async function mutateStorage(method, payload, message) {
    if (state.busy || !state.host) return;
    setBusy(true, message);
    showError('');
    try {
      const snapshot = await state.host[method](payload);
      // Keep the other form's unsaved edits and all configured platform rows.
      applySnapshot(snapshot);
      if (method === 'storeMedia') { populateMedia(); state.view = 'metadata'; }
      if (method === 'storeMetadata') { populateMetadata(); state.view = mediaDirty() ? 'media' : 'confirm'; }
      renderPendingMedia();
      render();
      announce('素材已储存，请继续核对原始素材。');
    } catch (error) { showError(errorText(error), true); }
    finally { setBusy(false); }
  }
  function readableSize(size) {
    if (!Number.isFinite(size) || size < 0) return '文件大小待确认';
    return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(size / 1024)} KB`;
  }
  function renderPendingMedia() {
    const list = $('pending-media-files');
    list.replaceChildren();
    Array.from($('media-files').files || []).forEach((file) => {
      const item = node('li', '', file.name);
      item.append(node('span', 'file-size', readableSize(file.size)));
      list.append(item);
    });
  }
  function selectedMediaKind() { return flow.kind(state.snapshot); }
  function populateMedia() {
    const video = selectedMediaKind() === 'video';
    $('media-files').value = '';
    $('media-files').multiple = !video;
    $('media-files').accept = video ? '.mp4,.mov,.mkv,.webm,video/*' : '.jpg,.jpeg,.png,.webp,image/*';
    document.querySelectorAll('input[name="content-kind"]').forEach((radio) => { radio.checked = radio.value === selectedMediaKind(); });
    $('media-file-hint').textContent = video ? '请选择恰好 1 个视频文件；不拆分、不转换。' : '按原选择顺序保留，不自动排序或转换；需要调整顺序请重新选择。';
    renderPendingMedia();
  }
  function renderKindHint() {
    const choice = document.querySelector('input[name="content-kind"]:checked')?.value;
    const matches = PLATFORMS.filter((p) => capability(p.id).available && flow.compatible(capability(p.id), choice));
    const saved = state.snapshot?.savedMediaKinds?.includes(choice) ? ' 该路径已有已保存素材，切换后可以继续使用。' : '';
    $('kind-route-hint').textContent = choice ? `自动匹配的发布包：${matches.map((p) => p.label).join('、') || '暂无已接入的对应发布包'}。${saved}` : '请选择图文或视频，不会自动代选。';
    $('select-kind-button').disabled = state.busy || !canEdit() || !choice;
  }
  async function selectKind() {
    if (state.busy || !canEdit()) return;
    const kind = document.querySelector('input[name="content-kind"]:checked')?.value;
    if (!kind) return;
    if (kind === selectedMediaKind()) { navigate('media'); return; }
    if (materialsDirty()) { showError('切换内容类型前，请先保存第 1、2 步的修改，或明确放弃本步修改。已保存的素材会保留。', true); return; }
    setBusy(true, '正在选择内容路径，不处理或分发素材。'); showError('');
    try {
      applySnapshot(await state.host.selectKind({sessionId: state.snapshot.sessionId, kind, assetRevision: state.snapshot.assetRevision}));
      state.view = 'media'; populateMedia(); render();
      announce('已切换内容路径，另一部分已保存素材仍保留；只调用所选类型的发布包。');
    } catch (error) { showError(errorText(error), true); }
    finally { setBusy(false); }
  }
  async function storeMedia() {
    if (!canEditMedia() || state.busy) return;
    const kind = selectedMediaKind();
    const files = Array.from($('media-files').files || []);
    if (!files.length && state.snapshot?.media) { navigate('metadata'); return; }
    if (!files.length || kind === 'video' && files.length !== 1) {
      showError(kind === 'video' ? '第一批视频必须恰好选择 1 个文件。' : '请先选择本篇图文的图片。', true);
      return;
    }
    const expected = kind === 'video' ? 'video/' : 'image/';
    if (files.some((file) => file.type && file.type !== 'application/octet-stream' && !file.type.startsWith(expected))) {
      showError('本批包含与所选素材类型不一致的文件，请重新选择；看板不会自动转换。', true);
      return;
    }
    await mutateStorage('storeMedia', { sessionId: state.snapshot.sessionId, kind, files }, '正在原样储存第一批素材，不处理内容。');
  }
  function populateMetadata() {
    const metadata = state.snapshot?.metadata;
    $('title-text').value = metadata?.title ?? '';
    $('body-text').value = metadata?.body ?? '';
    $('cover-absent').checked = metadata?.coverAbsent === true;
    $('keep-cover').checked = !!metadata?.cover;
    $('keep-cover-label').hidden = !metadata?.cover;
    $('keep-cover-text').textContent = metadata?.cover ? `保留已保存的封面：${metadata.cover.name}` : '保留已保存的封面';
    $('cover-file').value = '';
    ['title', 'body'].forEach((field) => {
      $(`${field}-file`).value = '';
      state[`${field}Source`] = null;
      state[`${field}Raw`] = null;
      renderTextSource(field);
    });
  }
  function renderTextSource(field) {
    const source = state[`${field}Source`];
    $(`${field}-text`).readOnly = !!source;
    $(`clear-${field}-file`).hidden = !source;
    $(`${field}-source-note`).textContent = source
      ? `已原样读取「${source}」。下方为原文，不会润色；需要修改可切换为手动输入。`
      : field === 'title'
        ? '也可直接在下方输入标题；选择 TXT 后仅原样读取，不分析或改写。'
        : 'TXT 内容会原样显示并保存，供你确认。';
  }
  async function readTextFile(field) {
    if (state.busy || !canEditMetadata()) return;
    const file = $(`${field}-file`).files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.txt') || file.size > 2 * 1024 * 1024) {
      showError('标题和正文文件请使用不超过 2 MB 的 UTF-8 TXT 文件；不会自动解码其他文档格式。', true);
      $(`${field}-file`).value = '';
      return;
    }
    setBusy(true, `仅原样读取${field === 'title' ? '标题' : '正文'} TXT，供你核对。`);
    try {
      const raw = await file.text();
      state[`${field}Raw`] = raw;
      $(`${field}-text`).value = raw;
      state[`${field}Source`] = file.name;
      renderTextSource(field);
      announce('TXT 原文已显示，尚未提交第二批储存。');
    } catch (error) { showError(errorText(error), true); }
    finally { setBusy(false); }
  }
  async function storeMetadata() {
    if (state.busy || !canEditMetadata()) return;
    const {cover, coverAbsent, keepCoverRevision, title, body} = metadataValues();
    // TXT originals are submitted from the raw string, not the textarea, because
    // native textareas may normalize CRLF newlines in their display value.
    // Do not trim or rewrite submitted strings. Trim is only an emptiness check.
    if (!title.trim() || !body.trim()) { showError('请分别提供标题和正文原文。', true); return; }
    if (cover && coverAbsent) { showError('封面文件与“不单独提供封面”不能同时选择。', true); return; }
    if (!cover && !coverAbsent && keepCoverRevision === null) { showError('请选择保留原封面、上传新封面，或明确不提供封面。', true); return; }
    if (!metadataDirty() && state.snapshot.metadata) { navigate('confirm'); return; }
    await mutateStorage('storeMetadata', { sessionId: state.snapshot.sessionId, cover, coverAbsent, title, body, keepCoverRevision }, '正在保存第二批修改，主素材保持不变。');
  }
  function enterMaterialEdit(batch) {
    navigate(batch === 1 ? 'media' : 'metadata');
  }
  async function confirmMaterials() {
    if (state.busy || !canEdit() || currentView() !== 'confirm' || materialsDirty()) return;
    if (state.snapshot.sourceConfirmed) { navigate('dashboard'); return; }
    setBusy(true, '正在提交素材确认，尚未调用平台发布包。');
    showError('');
    try {
      applySnapshot(await state.host.confirmMaterials({ sessionId: state.snapshot.sessionId, assetRevision: state.snapshot.assetRevision, confirmed: true }));
      state.view = 'dashboard'; render();
      announce('素材已确认，请选择平台、操作与时间。');
    } catch (error) { showError(errorText(error), true); }
    finally { setBusy(false); }
  }
  function previewUrl(value) {
    // Only host-owned previews. Do not automatically load unrelated remote URLs.
    if (typeof value !== 'string') return null;
    try {
      const url = new URL(value, window.location.href);
      if (url.protocol === 'blob:' || ['http:', 'https:'].includes(url.protocol) && url.origin === window.location.origin) return url.href;
    } catch { /* No preview is safer than an unknown URL. */ }
    return null;
  }
  function renderMaterials() {
    const box = $('materials-summary');
    box.replaceChildren();
    const snapshot = state.snapshot;
    if (!snapshot?.media || !snapshot.metadata) return;
    const media = node('div', 'summary-block');
    media.append(node('h3', '', snapshot.media.kind === 'video' ? '视频原文件' : '图片与原始顺序'));
    const files = node('ol', 'file-list');
    (snapshot.media.files || []).forEach((file) => {
      const item = node('li', '', file.name);
      item.append(node('span', 'file-size', readableSize(file.size)));
      files.append(item);
    });
    media.append(files);
    const cover = node('div', 'summary-block');
    cover.append(node('h3', '', '封面'));
    cover.append(node('p', 'cover-filename', snapshot.metadata.cover?.name || (snapshot.metadata.coverAbsent ? '已明确不单独提供封面' : '未收到封面信息')));
    const url = previewUrl(snapshot.metadata.cover?.previewUrl);
    if (url) { const image = node('img', 'cover-preview'); image.src = url; image.alt = '已储存的封面预览'; cover.append(image); }
    const title = node('div', 'summary-block full-width');
    title.append(node('h3', '', '标题原文'), node('pre', 'summary-text', snapshot.metadata.title));
    const body = node('div', 'summary-block full-width');
    body.append(node('h3', '', '正文原文'), node('pre', 'summary-text', snapshot.metadata.body));
    box.append(media, cover, title, body);
    $('materials-review-note').textContent = DASHBOARD_STAGES.has(currentStage()) ? '这批素材已进入后续流程；发布包只能处理本次确认绑定的素材版本。' : '核对顺序、封面和原文。确认前仍只做储存，不分发。';
  }
  function resetTiming(row, platform) {
    row.timingKind = null;
    row.region = platform.foreign ? null : 'china';
    row.timezone = platform.foreign ? null : 'Asia/Shanghai';
    row.date = '';
    row.minute = null;
    row.fold = null;
  }
  function option(value, text) {
    const entry = node('option', '', text);
    entry.value = value;
    return entry;
  }
  function editControl(control, intrinsicallyDisabled = false) {
    control.dataset.editControl = 'true';
    control.dataset.intrinsicDisabled = String(intrinsicallyDisabled);
    control.disabled = state.busy || !canConfigure() || intrinsicallyDisabled;
    return control;
  }
  function field(labelText, control, wide = false) {
    const wrapper = node('div', `field${wide ? ' full-width' : ''}`);
    const label = node('label', '', labelText);
    if (control.id) label.htmlFor = control.id;
    wrapper.append(label, control);
    return wrapper;
  }
  function radioChoice(name, value, labelText, checked, onChange, unavailable = false) {
    const label = node('label', 'choice');
    const input = editControl(node('input'), unavailable);
    input.type = 'radio'; input.name = name; input.value = value; input.checked = checked;
    input.dataset.focusKey = `${name}-${value}`;
    input.addEventListener('change', () => { if (!state.busy && canConfigure()) onChange(); });
    label.append(input, node('span', '', labelText));
    return label;
  }
  function rerenderPlatforms() {
    const focusKey = document.activeElement?.dataset.focusKey;
    renderPlatforms();
    updateReadiness();
    if (focusKey) Array.from(document.querySelectorAll('[data-focus-key]')).find((item) => item.dataset.focusKey === focusKey)?.focus();
  }
  function minuteLabel(minute) {
    if (!Number.isInteger(minute) || minute < 0 || minute > 1439) return '';
    return `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
  }
  function parseMinute(value) {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) return null;
    const [hour, minute] = value.split(':').map(Number);
    return hour * 60 + minute;
  }
  function appendTimezoneFields(container, row, platform) {
    if (!platform.foreign) {
      const zone = node('div', 'time-zone-label', '中国大陆 · Asia/Shanghai（北京时间）');
      container.append(field('发布时间时区', zone, true));
      return;
    }
    const region = editControl(node('select')); region.id = `${platform.id}-region`; region.dataset.focusKey = region.id;
    region.append(option('', '请选择时间区域'));
    REGIONS.forEach(([id, label]) => {
      const entry = option(id, id === 'local' && state.localZone ? `${label}（${state.localZone}）` : label);
      if (id === 'local' && !state.localZone) entry.disabled = true;
      region.append(entry);
    });
    region.value = row.region || '';
    region.addEventListener('change', () => {
      row.region = region.value || null;
      row.timezone = row.region === 'china' ? 'Asia/Shanghai' : row.region === 'local' ? state.localZone : null;
      row.fold = null;
      rerenderPlatforms();
    });
    container.append(field('时间区域', region));
    if (ZONES[row.region]) {
      const city = editControl(node('select')); city.id = `${platform.id}-timezone`; city.dataset.focusKey = city.id;
      city.append(option('', '请选择具体城市 / 时区'));
      ZONES[row.region].forEach(([zone, label]) => city.append(option(zone, `${label} · ${zone}`)));
      city.value = row.timezone || '';
      city.addEventListener('change', () => { row.timezone = city.value || null; row.fold = null; rerenderPlatforms(); });
      container.append(field('具体时区（含夏令时规则）', city));
    } else {
      container.append(field('具体时区', node('div', 'time-zone-label', row.timezone || '选择区域后显示；不会猜测跨区域的统一时区')));
    }
  }
  function renderTiming(body, row, platform, cap) {
    const box = node('div', 'schedule-fields');
    box.append(node('p', 'field-label', '什么时候发布？（必须明确选择）'));
    const group = node('div', 'choice-group'); group.setAttribute('role', 'radiogroup'); group.setAttribute('aria-label', `${platform.label}发布时间方式`);
    ['now', 'scheduled'].forEach((kind) => {
      const supported = modesFor(cap).includes(kind === 'now' ? 'publish' : 'schedule');
      group.append(radioChoice(`${platform.id}-timing`, kind, kind === 'now' ? '立即发布' : '定时发布', row.timingKind === kind, () => {
        row.timingKind = kind; row.date = ''; row.minute = null; row.fold = null; rerenderPlatforms();
      }, !supported));
    });
    box.append(group);
    const grid = node('div', 'row-fields');
    appendTimezoneFields(grid, row, platform);
    if (row.timingKind === 'scheduled') {
      const clearFold = () => {
        row.fold = null;
        const selector = $(`${platform.id}-fold`);
        if (selector) selector.value = '';
      };
      const date = editControl(node('input')); date.type = 'date'; date.id = `${platform.id}-date`; date.value = row.date;
      date.addEventListener('change', () => { row.date = date.value; clearFold(); updateReadiness(); });
      const exact = editControl(node('input')); exact.type = 'time'; exact.step = '60'; exact.id = `${platform.id}-time`; exact.value = minuteLabel(row.minute);
      const output = node('output', 'time-value', minuteLabel(row.minute) || '尚未设置'); output.id = `${platform.id}-time-value`;
      const slider = editControl(node('input')); slider.type = 'range'; slider.min = '0'; slider.max = '1439'; slider.step = '1'; slider.id = `${platform.id}-slider`;
      // The thumb needs a display position, but 12:00 is NOT a selected time.
      slider.value = String(row.minute ?? 720); slider.setAttribute('aria-describedby', output.id);
      slider.setAttribute('aria-valuetext', minuteLabel(row.minute) || '尚未设置时间，请拖动或使用方向键');
      slider.addEventListener('input', () => {
        row.minute = Number(slider.value); clearFold();
        exact.value = minuteLabel(row.minute); output.textContent = exact.value;
        slider.setAttribute('aria-valuetext', exact.value); updateReadiness();
      });
      exact.addEventListener('input', () => {
        row.minute = parseMinute(exact.value); clearFold();
        if (row.minute !== null) slider.value = String(row.minute);
        output.textContent = minuteLabel(row.minute) || '尚未设置';
        slider.setAttribute('aria-valuetext', output.textContent); updateReadiness();
      });
      grid.append(field('所选时区的日期', date), field('精确时间（24 小时制）', exact));
      const rangeField = field('拖拽设置该平台时间（精确到分钟）', slider, true);
      rangeField.prepend(output);
      const labels = node('div', 'range-labels'); labels.append(node('span', '', '00:00'), node('span', '', '12:00'), node('span', '', '23:59'));
      rangeField.append(labels, node('span', 'field-note', '拖动滑块，或使用上方时间输入。提交前会检查时间是否有效，以及是否符合平台排期限制。'));
      grid.append(rangeField);
      if (platform.foreign) {
        const details = node('details', 'dst-details full-width');
        details.append(node('summary', '', '夏令时重复时刻（通常无需设置）'));
        const fold = editControl(node('select')); fold.id = `${platform.id}-fold`;
        fold.append(option('', '遇到重复时刻时阻止，并由我明确选择'), option('0', '选第一次出现的时刻（较早）'), option('1', '选第二次出现的时刻（较晚）'));
        fold.value = row.fold === null ? '' : String(row.fold);
        fold.addEventListener('change', () => { row.fold = fold.value === '' ? null : Number(fold.value); updateReadiness(); });
        details.append(field('时钟回拨时如何解释所选时间', fold)); grid.append(details);
      }
    } else if (row.timingKind === 'now') {
      grid.append(node('p', 'field-note full-width', '立即发布不等待指定时刻；所选时区仅用于显示与执行摘要。'));
    }
    box.append(grid); body.append(box);
  }
  function renderPlatforms() {
    const list = $('platform-list'); list.replaceChildren();
    const unavailable = $('unavailable-platforms'); unavailable.replaceChildren();
    if (!state.snapshot) return;
    const kind = flow.kind(state.snapshot);
    const matched = PLATFORMS.filter((p) => capability(p.id).available && mediaCompatible(capability(p.id)));
    $('route-summary').textContent = `${kind === 'video' ? '视频' : '图文'}路径：已自动匹配${matched.map((p) => p.label).join('、') || '暂无对应'}发布包。${flow.committed(state.snapshot) ? '本次以已确认摘要为准，由 Agent 接手执行。' : '选择目标与操作后才会生成确认计划。'}`;
    PLATFORMS.forEach((platform) => {
      const row = state.rows.get(platform.id); const cap = capability(platform.id);
      const ready = cap.available === true && mediaCompatible(cap);
      if (!ready) {
        const reason = cap.available ? `现有包仅支持${cap.mediaKinds.includes('video') ? '视频' : '图文'}，不属于本次${kind === 'video' ? '视频' : '图文'}路径` : '尚未接入对应发布包与账号配置';
        unavailable.append(node('li', '', `${platform.label}：${reason}。`));
        return;
      }
      const card = node('article', `platform-card${row.selected ? ' selected' : ''}`);
      const header = node('div', 'platform-card-header');
      const label = node('label', 'platform-check');
      const checkbox = editControl(node('input')); checkbox.type = 'checkbox'; checkbox.checked = row.selected; checkbox.dataset.focusKey = `${platform.id}-selected`;
      checkbox.addEventListener('change', () => { row.selected = checkbox.checked; rerenderPlatforms(); });
      const name = node('span', 'platform-name', platform.label); name.append(node('span', 'platform-id', platform.id));
      label.append(checkbox, name);
      header.append(label, node('span', `badge ${ready ? 'ready' : 'blocked'}`, cap.available !== true ? '未接入 / 不可用' : mediaCompatible(cap) ? '发布包可用' : '不支持这批素材'));
      card.append(header);
      if (!row.selected) {
        const choices = node('div', 'choice-group quick-actions');
        choices.setAttribute('role', 'radiogroup'); choices.setAttribute('aria-label', `${platform.label}执行方式`);
        ['publish', 'draft'].forEach((action) => {
          const supported = action === 'draft' ? modesFor(cap).includes('draft') : modesFor(cap).some((mode) => ['publish', 'schedule'].includes(mode));
          choices.append(radioChoice(`${platform.id}-quick-action`, action, action === 'publish' ? '一键发布' : '一键草稿', false, () => {
            row.selected = true; row.action = action; row.acceptLocalDraft = false; resetTiming(row, platform); rerenderPlatforms();
          }, !supported));
        });
        card.append(choices);
      }
      if (row.selected) {
        const body = node('div', 'platform-body');
        if (!ready) body.append(node('div', 'message warning', cap.available === true ? '发布包已接入，但不支持本次素材类型；小红书需图文，YouTube 需视频。' : cap.reason || '尚未接入可执行的发布包；本次选择将阻止提交。'));
        const actionGroup = node('div', 'choice-group platform-actions');
        actionGroup.setAttribute('role', 'radiogroup'); actionGroup.setAttribute('aria-label', `${platform.label}执行方式`);
        ['publish', 'draft'].forEach((action) => {
          const supported = action === 'draft' ? modesFor(cap).includes('draft') : modesFor(cap).some((mode) => ['publish', 'schedule'].includes(mode));
          actionGroup.append(radioChoice(`${platform.id}-action`, action, action === 'publish' ? '一键发布' : '一键草稿', row.action === action, () => {
            if (row.action !== action) { row.action = action; row.acceptLocalDraft = false; resetTiming(row, platform); }
            rerenderPlatforms();
          }, !supported));
        });
        body.append(actionGroup);
        if (!row.action) body.append(node('p', 'platform-caption', '请选择“一键发布”或“一键草稿”，不会替你默认选择。'));
        const accounts = accountsFor(cap);
        const account = editControl(node('select')); account.id = `${platform.id}-account`;
        account.append(option('', accounts.length ? '请选择目标账号' : '暂无已配置的账号'));
        accounts.forEach((item) => account.append(option(item.id, item.label || item.id)));
        account.value = row.accountProfile;
        account.addEventListener('change', () => { row.accountProfile = account.value; row.accountConfirmed = false; rerenderPlatforms(); });
        const accountField = field('执行账号', account);
        if (accounts.length === 1) accountField.append(node('span', 'field-note', '已接入此本地账号配置；尚未检查平台登录，不代表已确认实际账号。'));
        body.append(accountField);
        if (cap.requireAccountConfirmation) {
          const label = node('label', 'check-line');
          const input = editControl(node('input')); input.type = 'checkbox'; input.checked = row.accountConfirmed;
          input.addEventListener('change', () => { row.accountConfirmed = input.checked; updateReadiness(); });
          label.append(input, node('span', '', '我确认使用上述账号配置；如执行时需要登录，我会核对实际账号。'));
          body.append(label);
        }
        const declarations = node('div', 'form-grid');
        postFields(cap).forEach((key) => {
          const spec = POST_FIELDS[key]; if (!spec) return;
          const select = editControl(node('select')); select.id = `${platform.id}-setting-${key}`;
          select.append(option('', '请选择，不会自动代填'));
          spec.options.forEach(([value, label], index) => select.append(option(String(index), label)));
          const index = spec.options.findIndex(([value]) => value === row.postSettings[key]);
          select.value = index < 0 ? '' : String(index);
          select.addEventListener('change', () => {
            if (select.value === '') delete row.postSettings[key];
            else row.postSettings[key] = spec.options[Number(select.value)][0];
            updateReadiness();
          });
          declarations.append(field(spec.label, select));
        });
        if (postFields(cap).length) body.append(declarations);
        if (row.action === 'draft') {
          // No time controls exist in the draft branch. Payload timing is always null.
          if (cap.draftScope === 'local') {
            const warning = node('div', 'message warning local-draft-warning');
            warning.append(node('p', '', '此发布包只能保存本地草稿包，不会进入该平台的草稿箱。'));
            const acceptLabel = node('label', 'check-line');
            const accept = editControl(node('input')); accept.type = 'checkbox'; accept.checked = row.acceptLocalDraft;
            accept.addEventListener('change', () => { row.acceptLocalDraft = accept.checked; updateReadiness(); });
            acceptLabel.append(accept, node('span', '', '我接受本地草稿；我知道这不是平台草稿箱。'));
            warning.append(acceptLabel); body.append(warning);
          } else if (cap.draftScope === 'platform') {
            body.append(node('p', 'platform-caption', '目标：该平台草稿箱。草稿不设置发布时间。'));
          } else body.append(node('div', 'message warning', '发布包未明确草稿保存位置，不能执行“一键草稿”。'));
        }
        if (row.action === 'publish') renderTiming(body, row, platform, cap);
        card.append(body);
      }
      list.append(card);
    });
  }
  function planErrors() {
    const errors = [];
    const selected = Array.from(state.rows.values()).filter((row) => row.selected);
    if (!selected.length) return ['请至少选择一个平台。'];
    selected.forEach((row) => {
      const cap = capability(row.platform); const name = labelFor(row.platform);
      if (cap.available !== true) errors.push(`${name}尚未接入可执行发布包`);
      if (!mediaCompatible(cap)) errors.push(`${name}不支持这批素材`);
      if (!accountsFor(cap).some((account) => account.id === row.accountProfile)) errors.push(`${name}需要有效账号`);
      if (cap.requireAccountConfirmation && !row.accountConfirmed) errors.push(`${name}需要确认账号配置`);
      postFields(cap).forEach((key) => {
        if (!POST_FIELDS[key]?.options.some(([value]) => value === row.postSettings[key])) errors.push(`${name}：${POST_FIELDS[key]?.label || key}`);
      });
      if (row.platform === 'youtube' && row.postSettings.made_for_kids === true && row.postSettings.notify_subscribers === true) {
        errors.push('YouTube 面向儿童的视频不能通知订阅者，请将“通知订阅者”改为“否”或重新确认受众');
      }
      if (!['draft', 'publish'].includes(row.action)) { errors.push(`${name}尚未选择操作`); return; }
      if (row.action === 'draft') {
        if (!modesFor(cap).includes('draft')) errors.push(`${name}不支持草稿`);
        if (!['platform', 'local'].includes(cap.draftScope)) errors.push(`${name}未明确草稿位置`);
        if (cap.draftScope === 'local' && !row.acceptLocalDraft) errors.push(`${name}需要确认接受本地草稿`);
        return;
      }
      if (!['now', 'scheduled'].includes(row.timingKind)) errors.push(`${name}尚未选择立即或定时`);
      if (!row.region || !row.timezone) errors.push(`${name}需要明确区域与具体时区`);
      if (ZONES[row.region] && !ZONES[row.region].some(([zone]) => zone === row.timezone)) errors.push(`${name}的具体时区无效`);
      if (row.region === 'china' && row.timezone !== 'Asia/Shanghai' || row.region === 'local' && row.timezone !== state.localZone) errors.push(`${name}的区域与时区不一致`);
      if (row.timingKind === 'now' && !modesFor(cap).includes('publish')) errors.push(`${name}不支持立即发布`);
      if (row.timingKind === 'scheduled') {
        if (row.platform === 'youtube' && row.postSettings.privacy !== 'public' && postFields(cap).includes('privacy')) errors.push('YouTube 定时发布需选择“公开”');
        if (!modesFor(cap).includes('schedule')) errors.push(`${name}不支持定时发布`);
        if (!/^\d{4}-\d{2}-\d{2}$/.test(row.date)) errors.push(`${name}需要发布日期`);
        if (!Number.isInteger(row.minute) || row.minute < 0 || row.minute > 1439) errors.push(`${name}需要具体发布时间`);
      }
    });
    return errors;
  }
  function updateReadiness() {
    const selected = Array.from(state.rows.values()).filter((row) => row.selected);
    $('selected-count').textContent = selected.length ? `已选择 ${selected.length} 个平台` : '尚未选择平台';
    $('prepare-plan-button').textContent = selected.length && selected.every((r) => r.action === 'draft') ? '确认保存草稿'
      : selected.length && selected.every((r) => r.action === 'publish') ? '确认发布' : '核对并确认执行';
    const errors = planErrors();
    $('plan-validation-message').textContent = !canConfigure()
      ? currentStage() === 'AWAIT_PLAN_CONFIRMATION' && !state.review
        ? '已有待确认方案，但摘要暂未载入，请刷新进度；本页不会重复创建或执行。'
        : state.review ? '确认摘要已锁定；选择“否”可返回调整。' : '当前阶段不能修改或再次提交发布方案。'
      : errors.length ? `尚需完成：${errors.join('；')}。` : '配置已填写完整。点击“发布”生成确认摘要；此时仍不会执行分发。';
    $('prepare-plan-button').disabled = state.busy || !canConfigure() || errors.length > 0;
  }
  function buildRows() {
    return PLATFORMS.map((platform) => state.rows.get(platform.id)).filter((row) => row.selected).map((row) => ({
      platform: row.platform, selected: true, action: row.action,
      timing: row.action === 'draft' ? null : {
        kind: row.timingKind, region: row.region, timezone: row.timezone,
        date: row.timingKind === 'scheduled' ? row.date : null,
        minute: row.timingKind === 'scheduled' ? row.minute : null,
        fold: row.timingKind === 'scheduled' ? row.fold : null
      },
      accountProfile: row.accountProfile,
      accountConfirmed: row.accountConfirmed,
      postSettings: {...row.postSettings},
      acceptLocalDraft: row.action === 'draft' && row.acceptLocalDraft
    }));
  }
  async function preparePlan() {
    if (state.busy || !canConfigure()) return;
    const errors = planErrors();
    if (errors.length) { showError(errors.join('；'), true); return; }
    const rows = buildRows();
    setBusy(true, '正在生成逐项确认摘要，尚未执行发布或保存草稿。');
    showError('');
    try {
      const review = validateReview(await state.host.preparePlan({ sessionId: state.snapshot.sessionId, assetRevision: state.snapshot.assetRevision, rows }));
      if (review.rows.length !== rows.length || review.rows.some((row, index) => row.platform !== rows[index].platform)) {
        throw new Error('确认摘要的平台或顺序与本次选择不一致，请取消此方案并核对，不能执行。');
      }
      state.review = review;
      state.finalAttempted = false;
      $('confirmation-error').hidden = true;
      render();
      announce('请核对最终摘要；选择“是”会保存已确认任务，再由 Agent 接手执行。');
    } catch (error) { showError(errorText(error), true); }
    finally { setBusy(false); }
  }
  function renderReviewRows(list, review) {
    list.replaceChildren();
    (review?.rows || []).forEach((row) => {
      const card = node('section', 'confirmation-row');
      card.append(node('h3', '', row.label || labelFor(row.platform)));
      const pairs = [['操作', row.actionLabel], ['时间', row.timeLabel], ['账号', row.accountLabel]];
      if (row.draftScope) pairs.push(['草稿位置', row.draftScope === 'local' ? '本地草稿包，不是平台草稿箱' : row.draftScope === 'platform' ? '平台草稿箱' : row.draftScope]);
      if (row.notice) pairs.push(['提示', row.notice]);
      const details = node('dl');
      pairs.forEach(([label, value]) => details.append(node('dt', '', label), node('dd', '', plain(value) || '未提供')));
      card.append(details); list.append(card);
    });
  }
  function renderConfirmation() {
    const dialog = $('confirmation-dialog');
    if (!state.review) { if (dialog.open) dialog.close(); return; }
    const review = state.review;
    $('confirmation-question').textContent = review.question;
    renderReviewRows($('confirmation-rows'), review);
    const notices = $('confirmation-notices'); notices.replaceChildren();
    review.notices.forEach((notice) => notices.append(node('li', '', plain(notice))));
    renderReceiver();
    if (!dialog.open) dialog.showModal();
  }
  async function confirmPlan(confirmed) {
    if (state.busy || !state.host || !state.connected || !state.review || state.finalAttempted || confirmed && state.remoteChanged) return;
    const review = state.review;
    if (confirmed) state.finalAttempted = true;
    setBusy(true, confirmed ? '正在保存已确认任务，等待交给 Agent；本页不会执行发布。' : '正在取消这次确认，保留看板设置。');
    $('confirmation-error').hidden = true;
    try {
      const snapshot = await state.host.confirmPlan({ sessionId: review.sessionId, planId: review.planId, planHash: review.planHash, confirmed });
      applySnapshot(snapshot);
      announce(confirmed ? (flow.executionPresentation(snapshot)?.title || '任务记录已更新，请核对状态；本页不会自行执行。') : '已取消本次确认，可以重新调整。');
    } catch (error) {
      $('confirmation-error').textContent = confirmed
        ? `确认记录返回不确定：${errorText(error)}。任务可能已保存，请刷新进度核对；不要重复点击“是”或重新创建任务。`
        : `未确认取消成功：${errorText(error)}。当前方案仍保持锁定，可再次取消或刷新状态。`;
      $('confirmation-error').hidden = false;
    } finally { setBusy(false); }
  }
  async function newSession() {
    if (state.busy || !state.connected || currentStage() !== 'FINISHED' || typeof state.host?.newSession !== 'function') return;
    const previousSessionId = state.snapshot.sessionId;
    setBusy(true, '正在新建下一批素材会话，不会重新提交上一批。');
    showError('');
    try {
      // Keep this call before the first await so the portable bridge can inspect
      // navigator.userActivation for the user's actual button click.
      const snapshot = await state.host.newSession();
      if (snapshot?.stage !== 'AWAIT_KIND' || snapshot.sessionId === previousSessionId) {
        throw new Error('未能建立新素材批次。原批次保持不变，请刷新进度。');
      }
      $('media-files').value = '';
      state.webUpload = false;
      $('media-files').multiple = true;
      $('media-files').accept = 'image/*';
      $('media-file-hint').textContent = '按原选择顺序保留，不自动排序或转换；需要调整顺序请重新选择。';
      applySnapshot(snapshot, true);
      renderPendingMedia();
      announce('已进入独立的新素材批次，请先选择图文或视频。');
    } catch (error) { showError(errorText(error), true); }
    finally { setBusy(false); }
  }
  function renderResults() {
    const results = state.snapshot?.results || [];
    const presentation = flow.executionPresentation(state.snapshot);
    const visible = results.length > 0 || flow.committed(state.snapshot);
    setVisible('results-section', visible);
    setVisible('new-session-button', currentStage() === 'FINISHED' && typeof state.host?.newSession === 'function');
    $('results-heading').textContent = presentation?.title || '任务状态';
    $('execution-note').textContent = presentation?.message || '以下为各平台的实际执行结果。';
    setVisible('agent-handoff', state.snapshot?.executionMode === 'agent' && presentation?.showHandoff === true);
    $('agent-handoff-instruction').textContent = currentStage() === 'PAUSED'
      ? '请在 Agent 聊天中让它核对暂停原因与已有结果，不要重复发布。'
      : flow.receiver(state.snapshot).status === 'busy'
        ? '接收器正在处理任务，请勿重复提交。如本批次长时间未接手，请回到 Agent 应用核对。'
        : '请回到 Agent 应用，要求启用当前素材批次的接收器；无需重新填写或确认相同设置。';
    const handoff = flow.handoffText(state.snapshot);
    if ($('agent-handoff-text').value !== handoff) {
      $('agent-handoff-text').value = handoff;
      $('handoff-copy-message').textContent = '';
    }
    setVisible('handoff-details', !!handoff);
    setVisible('handoff-unavailable', !handoff);
    $('copy-handoff-button').disabled = !handoff;
    setVisible('result-receipt-note', results.length > 0);
    const list = $('result-list'); list.replaceChildren();
    results.forEach((result) => {
      const item = node('li'); const header = node('div', 'result-heading');
      header.append(node('strong', '', labelFor(result.platform)), node('span', 'result-code', plain(result.status) || '未返回状态'));
      item.append(header);
      if (result.message) item.append(node('p', 'result-message', plain(result.message)));
      if (typeof result.url === 'string') {
        try {
          const url = new URL(result.url);
          if (['http:', 'https:'].includes(url.protocol)) {
            const link = node('a', '', '查看平台结果'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; item.append(link);
          }
        } catch { item.append(node('p', 'result-message', '返回的链接格式无效，请核对平台回执。')); }
      }
      list.append(item);
    });
  }
  function renderReceiver() {
    const receiver = flow.receiver(state.snapshot);
    const message = !state.connected ? '暂时无法确认 Agent 接收状态。可以恢复连接后查看；面板不会自行启动接收器或发布。'
      : state.remoteChanged ? '素材或任务记录已有变化，接收状态需刷新后重新核对。本页输入仍保留，当前不能提交旧方案。'
        : `${receiver.title}。${receiver.message}`;
    for (const id of ['agent-receiver-status', 'confirmation-receiver-status']) {
      $(id).textContent = message;
      $(id).className = `message ${state.connected && !state.remoteChanged ? receiver.tone : 'warning'}`;
    }
  }
  function renderConnection() {
    const banner = $('connection-status');
    if (!state.host) {
      banner.className = 'connection-banner warning';
      banner.textContent = '看板暂未连接，请启动一键发布程序后刷新。连接恢复前不能储存或发布。';
    } else if (!state.snapshot || !state.connected) {
      banner.className = 'connection-banner warning';
      banner.textContent = '看板连接中断，已储存的素材和任务会保留。无法读取进度不代表 Agent 已停止；请到 Agent 应用核对。';
    } else {
      banner.className = 'connection-banner connected';
      const views = {kind: '选择内容类型', media: '上传或修改主素材', metadata: '上传或修改封面与文案', confirm: '核对素材', dashboard: STAGE_LABELS[currentStage()]};
      banner.textContent = `当前步骤：${views[currentView()]}。`;
    }
    $('session-label').hidden = true;
    $('session-label').textContent = '';
    const kind = flow.kind(state.snapshot);
    const matches = PLATFORMS.filter((p) => capability(p.id).available && (!kind || mediaCompatible(capability(p.id))));
    $('platform-connections').textContent = state.connected && state.snapshot
      ? `${kind ? (kind === 'video' ? '视频' : '图文') + '路径 · 自动匹配' : '已接入'}：${matches.map((p) => p.label).join('、') || '暂无对应发布包'}（由 Agent 接手后检查登录）` : '';
    renderReceiver();
  }
  function updateLocks() {
    $('kind-fields').disabled = state.busy || !canEdit();
    $('media-fields').disabled = state.busy || !canEditMedia();
    $('metadata-fields').disabled = state.busy || !canEditMetadata();
    $('cover-file').disabled = state.busy || !canEditMetadata() || $('cover-absent').checked;
    $('keep-cover').disabled = state.busy || !canEditMetadata() || !state.snapshot?.metadata?.cover;
    const canConfirm = !state.busy && canEdit() && currentView() === 'confirm' && !materialsDirty();
    $('confirm-materials-button').disabled = !canConfirm;
    ['edit-media-button', 'edit-metadata-button'].forEach((id) => { $(id).disabled = state.busy || !state.connected || !!state.review; });
    document.querySelectorAll('[data-edit-control]').forEach((control) => {
      control.disabled = state.busy || !canConfigure() || control.dataset.intrinsicDisabled === 'true';
    });
    $('refresh-button').disabled = state.busy;
    $('confirm-no-button').disabled = state.busy || !state.connected || state.finalAttempted;
    $('confirm-yes-button').disabled = state.busy || !state.connected || state.finalAttempted || state.remoteChanged;
    $('confirm-refresh-button').hidden = !state.finalAttempted;
    $('confirm-refresh-button').disabled = state.busy;
    $('new-session-button').disabled = state.busy || !state.connected || currentStage() !== 'FINISHED';
    document.querySelectorAll('[data-go-step]').forEach((control) => {
      const step = control.dataset.goStep === 'media' && !flow.kind(state.snapshot) ? 'kind' : control.dataset.goStep;
      control.disabled = state.busy || !state.connected || !!state.review || state.finalAttempted || !flow.canVisit(state.snapshot, step);
      control.title = control.disabled ? '请先完成前面的素材步骤；最终确认窗口中请先选择“否”返回。' : '返回此步骤，已保存内容不会丢失';
    });
    $('store-media-button').textContent = state.snapshot?.media ? (mediaDirty() ? '保存新主素材，下一步' : '保留主素材，下一步') : '储存主素材，下一步';
    $('store-metadata-button').textContent = state.snapshot?.metadata ? (metadataDirty() ? '保存文案与封面修改，下一步' : '保留文案与封面，下一步') : '一起储存封面、标题和正文';
    renderKindHint();
    updateReadiness();
  }
  function render() {
    const stage = currentStage();
    const view = currentView();
    const committed = flow.committed(state.snapshot);
    const materialView = ['kind', 'media', 'metadata'].includes(view);
    const webUpload = state.webUpload || committed;
    renderConnection();
    setVisible('chat-materials-section', materialView && !webUpload);
    setVisible('web-upload-mode', materialView && webUpload && !committed);
    setVisible('kind-section', view === 'kind' && webUpload);
    setVisible('media-section', view === 'media' && webUpload);
    setVisible('metadata-section', view === 'metadata' && webUpload);
    setVisible('cancel-media-edit', view === 'media' && !!state.snapshot?.media && canEdit());
    setVisible('cancel-metadata-edit', view === 'metadata' && !!state.snapshot?.metadata && canEdit());
    setVisible('materials-review-section', view === 'confirm');
    setVisible('materials-review-actions', view === 'confirm');
    setVisible('dashboard-section', view === 'dashboard');
    setVisible('agent-materials-help', !committed);
    setVisible('configuration-controls', !committed);
    setVisible('confirmed-plan', committed);
    if (committed) {
      renderReviewRows($('confirmed-plan-rows'), state.snapshot.review);
      const notices = $('confirmed-plan-notices'); notices.replaceChildren();
      (state.snapshot.review?.notices || []).forEach((notice) => notices.append(node('li', '', plain(notice))));
      if (!state.snapshot.review?.rows?.length) $('confirmed-plan-rows').append(node('p', 'subtle', '已确认摘要暂未返回，请刷新进度；不会据此重新生成或提交任务。'));
    }
    $('dashboard-heading').textContent = committed ? '本次已确认的分发任务' : '选择这次要去的平台';
    $('dashboard-description').textContent = committed ? '所选素材、平台、操作与时间已锁定，Agent 读取这些字段直接交给发布包，无需在聊天重填。' : '在这里选择平台、发布／草稿、时间、时区与最终许可。已就绪的 Agent 接收器会接收确认结果；面板不执行平台发布。';
    $('navigation-note').textContent = committed
      ? '本批次已最终确认，各步骤可返回查看，但不能再修改这份授权绑定的素材和计划；返回或刷新不会取消、暂停或重新提交。'
      : '点击步骤可返回修改。未保存的输入会在本页面保留；修改素材后需要重新确认。';
    const files = state.snapshot?.media?.files || [];
    const video = selectedMediaKind() === 'video';
    $('chat-materials-status').textContent = !state.snapshot?.media
      ? `等待 Agent 导入${selectedMediaKind() ? (video ? '视频' : '图文') : '本次'}主素材。已在聊天上传后，点击下方按钮刷新。`
      : !state.snapshot.metadata ? `主素材已保存：${files.map((file) => file.name).join('、')}。请在聊天提供第二批封面／标题／正文，再刷新。`
        : '两批素材均已保存。可返回第 3 步核对；如需修改，请在聊天告诉 Agent 要替换哪一部分，另一部分保持不变。';
    $('media-heading').textContent = video ? '上传或修改视频' : '上传或修改图文图片';
    $('media-route-hint').textContent = video ? '视频路径：一次只上传一个视频。封面将在第 2 步单独提供。' : '图文路径：一次上传本篇全部图片。不会接收视频或自动转换格式。';
    $('stored-media-summary').hidden = !files.length;
    $('stored-media-summary').textContent = files.length ? `已保存：${files.map((file) => file.name).join('、')}。无需修改时可直接下一步；选择新文件会替换本路径主素材，封面与文案保持不变。` : '';
    $('first-batch-summary').textContent = files.length ? `第一批已储存 ${files.length} 个文件：${files.map((file) => file.name).join('、')}` : '';
    $('stage-badge').textContent = state.review ? '等待最终确认' : STAGE_LABELS[stage] || '';
    const step = view === 'kind' ? 'media' : view;
    const stepOrder = ['media', 'metadata', 'confirm', 'dashboard'];
    document.querySelectorAll('[data-step]').forEach((item) => {
      if (item.dataset.step === step) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
      const button = item.querySelector('button');
      if (item.dataset.step === step) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
      item.classList.toggle('complete', stepOrder.indexOf(item.dataset.step) < stepOrder.indexOf(step));
    });
    renderMaterials(); renderPlatforms(); renderResults(); renderConfirmation(); updateLocks(); scheduleProgressRefresh();
  }

  async function copyHandoff() {
    const text = flow.handoffText(state.snapshot);
    if (!text) return;
    try {
      // Explicit user click only; never send this text to another application.
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(text);
      $('handoff-copy-message').textContent = '已复制。请自行粘贴到 Agent 聊天；本页没有发送消息或启动发布。';
    } catch {
      $('agent-handoff-text').focus(); $('agent-handoff-text').select();
      $('handoff-copy-message').textContent = '浏览器未允许直接复制，已选中文本，请手动复制到 Agent 聊天。';
    }
  }

  $('refresh-button').addEventListener('click', refreshSnapshot);
  $('refresh-materials-button').addEventListener('click', refreshSnapshot);
  $('use-web-upload-button').addEventListener('click', () => { state.webUpload = true; render(); });
  $('use-chat-upload-button').addEventListener('click', () => {
    if (materialsDirty()) { showError('本页有未保存的素材修改，请先保存或明确放弃；当前输入会保留。', true); return; }
    state.webUpload = false; render();
  });
  document.querySelectorAll('[data-go-step]').forEach((control) => control.addEventListener('click', () => navigate(control.dataset.goStep)));
  document.querySelectorAll('input[name="content-kind"]').forEach((input) => input.addEventListener('change', renderKindHint));
  $('select-kind-button').addEventListener('click', selectKind);
  $('media-files').addEventListener('change', () => { renderPendingMedia(); updateLocks(); });
  $('store-media-button').addEventListener('click', storeMedia);
  $('store-metadata-button').addEventListener('click', storeMetadata);
  $('cover-absent').addEventListener('change', () => { if ($('cover-absent').checked) { $('cover-file').value = ''; $('keep-cover').checked = false; } updateLocks(); });
  $('cover-file').addEventListener('change', () => { if ($('cover-file').files.length) { $('cover-absent').checked = false; $('keep-cover').checked = false; } updateLocks(); });
  $('keep-cover').addEventListener('change', () => { if ($('keep-cover').checked) { $('cover-file').value = ''; $('cover-absent').checked = false; } updateLocks(); });
  ['title-text', 'body-text'].forEach((id) => $(id).addEventListener('input', updateLocks));
  ['title', 'body'].forEach((field) => {
    $(`${field}-file`).addEventListener('change', () => readTextFile(field));
    $(`clear-${field}-file`).addEventListener('click', () => {
      if (state.busy) return;
      $(`${field}-file`).value = ''; state[`${field}Source`] = null; state[`${field}Raw`] = null; renderTextSource(field); $(`${field}-text`).focus();
    });
  });
  $('confirm-materials-button').addEventListener('click', confirmMaterials);
  $('edit-media-button').addEventListener('click', () => enterMaterialEdit(1));
  $('edit-metadata-button').addEventListener('click', () => enterMaterialEdit(2));
  ['cancel-media-edit', 'cancel-metadata-edit'].forEach((id) => $(id).addEventListener('click', () => {
    if (state.busy) return;
    if (id === 'cancel-media-edit') populateMedia(); else populateMetadata();
    state.view = mediaDirty() ? 'media' : metadataDirty() ? 'metadata' : flow.defaultView(state.snapshot);
    showError(''); render(); announce('已放弃本步尚未保存的修改，已保存的素材保持不变。');
  }));
  $('prepare-plan-button').addEventListener('click', preparePlan);
  $('confirm-no-button').addEventListener('click', () => confirmPlan(false));
  $('confirm-yes-button').addEventListener('click', () => confirmPlan(true));
  $('confirm-refresh-button').addEventListener('click', refreshSnapshot);
  $('new-session-button').addEventListener('click', newSession);
  $('copy-handoff-button').addEventListener('click', copyHandoff);
  $('confirmation-dialog').addEventListener('cancel', (event) => { event.preventDefault(); if (!state.busy && !state.finalAttempted) confirmPlan(false); });
  render();
  // Initial read is allowed; this never starts a platform action or content analysis.
  refreshSnapshot();
})();
