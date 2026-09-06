/* Shared, side-effect-free navigation and content-route rules. */
((root) => {
  const editableStages = new Set(['AWAIT_KIND', 'AWAIT_MEDIA', 'AWAIT_METADATA', 'AWAIT_MATERIAL_CONFIRMATION', 'CONFIGURING']);
  // A confirmed handoff is already immutable, even before an Agent takes it.
  const committedStages = new Set(['AWAIT_AGENT', 'EXECUTING', 'FINISHED', 'PAUSED']);
  const kind = (snapshot) => snapshot?.contentKind || snapshot?.media?.kind || null;
  const editable = (snapshot) => editableStages.has(snapshot?.stage);
  const compatible = (cap, contentKind) => !!contentKind && Array.isArray(cap?.mediaKinds) && cap.mediaKinds.includes(contentKind);
  function defaultView(snapshot) {
    if (committedStages.has(snapshot?.stage) || snapshot?.stage === 'AWAIT_PLAN_CONFIRMATION') return 'dashboard';
    if (!kind(snapshot)) return 'kind';
    if (!snapshot?.media) return 'media';
    if (!snapshot?.metadata) return 'metadata';
    return snapshot.sourceConfirmed ? 'dashboard' : 'confirm';
  }
  function canVisit(snapshot, step) {
    if (!snapshot) return false;
    if (step === 'kind') return true;
    if (step === 'media') return !!kind(snapshot);
    if (step === 'metadata') return !!snapshot.media || !!snapshot.metadata;
    if (step === 'confirm') return !!snapshot.media && !!snapshot.metadata;
    if (step === 'dashboard') return (!!snapshot.media && snapshot.sourceConfirmed === true) || committedStages.has(snapshot.stage) || snapshot.stage === 'AWAIT_PLAN_CONFIRMATION';
    return false;
  }
  const committed = (snapshot) => committedStages.has(snapshot?.stage);
  function receiver(snapshot) {
    if (snapshot?.stage === 'FINISHED') return {
      status: 'finished', ready: false, tone: 'info', title: '本批次已完成接收与处理',
      message: '一次性接收器退出属于正常状态，无需重新启用或再次提交。发布、草稿或排期是否成功，请以下方实际回执为准。',
    };
    if (snapshot?.stage === 'PAUSED') return {
      status: 'paused', ready: false, tone: 'warning', title: '请处理本批次的暂停原因',
      message: '请让 Agent 根据下方实际回执核对暂停原因和已有结果；不要重新提交或直接重发，重新启用接收器也不会自动修复本次任务。',
    };
    if (snapshot?.stage === 'EXECUTING') return {
      status: 'executing', ready: false, tone: 'info', title: '本批次已由 Agent 接手',
      message: '接收器已完成交接职责，请查看任务进度和实际回执。不要因为接收器退出而重新提交；面板仍只读取进度。',
    };
    const value = snapshot?.agentReceiver;
    const current = value && value.sessionId === snapshot?.sessionId && value.assetRevision === snapshot?.assetRevision;
    const reasons = {
      not_connected: '当前素材版本尚未启用接收器。',
      expired: '接收器的等待时间已到，或最近未能确认它仍在线。',
      materials_changed: '素材版本已变更，需要 Agent 为当前版本启用接收器。',
      process_unverifiable: '暂时无法确认 Agent 接收进程是否仍在运行。',
      process_stopped: 'Agent 接收进程已停止。',
      not_owned: '尚未确认当前接收进程持有这批任务的接收权限。',
    };
    const reason = !current ? (value ? reasons.materials_changed : reasons.not_connected)
      : reasons[value.reason] || '暂时无法确认当前素材版本的 Agent 接收器已就绪。';
    if (!current || !['ready', 'busy'].includes(value.status)) return {
      status: 'offline', ready: false, tone: 'warning',
      title: 'Agent 接收器尚未就绪',
      message: `${reason}可以先保存最终确认；请回到 Agent 应用核对并启用接收器，无需重填设置或重复提交。`,
    };
    if (value.status === 'busy') return {
      status: 'busy', ready: false, tone: 'info', title: 'Agent 接收器忙碌中',
      message: '接收器正在处理任务。以本批次实际执行状态为准；无需重新填写或重复提交设置。',
    };
    return {
      status: 'ready', ready: true, tone: 'info', title: 'Agent 接收器已就绪',
      message: '已准备接收当前素材版本的最终确认。确认后通常数秒接手，无需复制或重新填写平台、操作和时间；看板不执行平台发布。',
    };
  }
  function executionPresentation(snapshot) {
    const stage = snapshot?.stage;
    if (stage === 'AWAIT_AGENT' && receiver(snapshot).ready) return {
      title: '发布指令已保存，等待 Agent 接收',
      message: 'Agent 接收器已就绪，通常数秒内接手。此刻尚未启动发布，无需复制或重填设置；接手后将显示实际进度。面板不执行平台发布。',
      showHandoff: false,
    };
    if (stage === 'AWAIT_AGENT') return {
      title: '发布指令已保存，等待 Agent 接手',
      message: receiver(snapshot).status === 'busy'
        ? 'Agent 接收器正在处理任务，本批次尚未开始执行。无需重填设置或再次提交；如长时间未接手，请回到 Agent 应用核对。'
        : '尚未启动发布，当前素材版本的 Agent 接收器未就绪。请回到 Agent 应用启用接收器，再接收这份已确认任务；无需重填设置或再次提交。',
      showHandoff: true,
    };
    if (stage === 'EXECUTING') return {
      title: snapshot.executionMode === 'agent' ? 'Agent 正在执行发布任务' : '执行任务进行中',
      message: '请以 Agent 的进度和下方实际回执为准。刷新、关闭或重新打开看板只影响显示，不会取消、暂停或重新提交发布任务。',
      showHandoff: false,
    };
    if (stage === 'PAUSED') return {
      title: '执行需要处理',
      message: '请回到 Agent 应用核对暂停原因和已完成的平台。刷新看板不会恢复或重发；需先核实已有结果，再由 Agent 处理。',
      showHandoff: true,
    };
    if (stage === 'FINISHED') return {
      title: '本批次执行流程已结束',
      message: '请逐项查看实际回执；流程结束不代表所有平台都已公开发布。刷新或返回查看不会重新执行。',
      showHandoff: false,
    };
    return null;
  }
  function handoffText(snapshot) {
    const handoff = snapshot?.agentHandoff;
    if (snapshot?.executionMode !== 'agent' || !committed(snapshot) || !handoff || handoff.sessionId !== snapshot.sessionId
      || !['workspace', 'sessionId', 'planHash'].every((key) => typeof handoff[key] === 'string' && handoff[key].trim())) return '';
    // Never copy the whole snapshot or arbitrary handoff fields: they can include
    // app-specific data. These are identifiers, not authorization credentials.
    const locator = {workspace: handoff.workspace, sessionId: handoff.sessionId, planHash: handoff.planHash};
    const request = snapshot.stage === 'PAUSED' ? '请核对已确认发布任务的暂停原因与已有结果，不要重复发布。' : '执行已确认的发布任务。';
    return `${request}\n请读取以下本机工作目录中的已保存素材和已确认计划，核验授权与执行记录后接手；以下定位信息不是新的发布授权。\n${JSON.stringify(locator, null, 2)}`;
  }
  const api = {kind, editable, compatible, defaultView, canVisit, committed, receiver, executionPresentation, handoffText};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.OneClickPublishWorkflow = api;
})(typeof globalThis === 'undefined' ? this : globalThis);
