/* Portable loopback bridge. A native host may inject the same protocol first. */
(() => {
  "use strict";
  if (window.OneClickPublishHost) return;
  const SESSION_KEY = "one-click-publish.session.v1";
  const FALLBACK_CHUNK_BYTES = 4 * 1024 * 1024;
  let bootstrapPromise;
  let currentSessionId = null;
  let currentSnapshot = null;

  class HostError extends Error {
    constructor(code, message) {
      super(message);
      this.name = "OneClickPublishHostError";
      this.code = code;
    }
  }

  function assertLocalHost() {
    if (location.protocol !== "http:" || location.hostname !== "127.0.0.1") {
      throw new HostError("LOCAL_SERVICE_REQUIRED", "请先启动本包的本地服务，再使用它输出的 127.0.0.1 地址打开看板。直接打开 HTML 不会分发内容。");
    }
  }

  async function decode(response) {
    let payload;
    try { payload = await response.json(); }
    catch (_) { throw new HostError("INVALID_SERVER_RESPONSE", "本地服务没有返回有效回执，请检查它是否仍在运行。"); }
    if (!response.ok || payload?.ok === false) {
      throw new HostError(payload?.error?.code || "LOCAL_REQUEST_FAILED", payload?.error?.message || "本地请求未完成。");
    }
    return payload;
  }

  function bootstrap() {
    assertLocalHost();
    if (!bootstrapPromise) {
      bootstrapPromise = fetch("/api/bootstrap", { credentials: "same-origin", cache: "no-store", redirect: "error" })
        .then(decode).then(result => {
          if (result.protocolVersion !== "1" || !result.csrfToken) {
            throw new HostError("PROTOCOL_MISMATCH", "看板与本地服务版本不匹配。");
          }
          return result;
        }).catch(error => { bootstrapPromise = null; throw error; });
    }
    return bootstrapPromise;
  }

  async function request(path, { method = "POST", json, raw, headers = {} } = {}) {
    const config = await bootstrap();
    const requestHeaders = { "X-OCP-CSRF": config.csrfToken, ...headers };
    let body;
    if (raw !== undefined) {
      requestHeaders["Content-Type"] = "application/octet-stream";
      body = raw;
    } else if (json !== undefined) {
      requestHeaders["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    }
    try {
      return await decode(await fetch(path, {
        method, headers: requestHeaders, body,
        credentials: "same-origin", cache: "no-store", redirect: "error",
      }));
    } catch (error) {
      if (error instanceof HostError) throw error;
      throw new HostError("LOCAL_SERVICE_UNREACHABLE", "与本地服务的连接中断。已上传部分仍保留，但不会标记成功或自动重试发布，请恢复服务后查看会话。");
    }
  }

  function remember(snapshot) {
    if (snapshot?.sessionId) {
      currentSnapshot = snapshot;
      currentSessionId = snapshot.sessionId;
      try { localStorage.setItem(SESSION_KEY, currentSessionId); } catch (_) { /* Memory fallback. */ }
    }
    return snapshot;
  }

  function sessionId(value) {
    const result = value || currentSessionId;
    if (typeof result !== "string" || !result) throw new HostError("SESSION_REQUIRED", "请先载入素材会话。");
    return result;
  }

  function assertFile(file) {
    if (!(file instanceof File) || file.size < 1) {
      throw new HostError("FILE_REQUIRED", "请从本机选择非空的实际文件；不接受客户端提供的本地路径。");
    }
  }

  async function uploadFile(id, file, purpose) {
    assertFile(file);
    const config = await bootstrap();
    if (file.size > config.maxUploadBytes) throw new HostError("FILE_TOO_LARGE", `“${file.name}”超过本地服务允许的文件大小。`);
    const begun = await request("/api/uploads/begin", { json: {
      sessionId: id, purpose, filename: file.name, size: file.size,
    } });
    const chunkBytes = Math.min(begun.chunkBytes || FALLBACK_CHUNK_BYTES, FALLBACK_CHUNK_BYTES);
    let offset = begun.offset;
    while (offset < file.size) {
      const piece = file.slice(offset, Math.min(offset + chunkBytes, file.size));
      const ack = await request(`/api/uploads/chunk?id=${encodeURIComponent(begun.uploadId)}&sessionId=${encodeURIComponent(id)}`, {
        method: "PUT", raw: piece, headers: { "X-Upload-Offset": String(offset) },
      });
      if (ack.offset !== offset + piece.size) throw new HostError("UPLOAD_OFFSET_MISMATCH", "服务端回执与上传进度不符，本批素材未入库。");
      offset = ack.offset;
      window.dispatchEvent(new CustomEvent("one-click-publish:upload-progress", {
        detail: { sessionId: id, purpose, filename: file.name, loaded: offset, total: file.size },
      }));
    }
    const finished = await request("/api/uploads/finish", { json: { sessionId: id, uploadId: begun.uploadId } });
    if (finished.finished !== true) throw new HostError("UPLOAD_INCOMPLETE", "文件未完整上传，本批素材未入库。");
    return begun.uploadId;
  }

  const host = {
    protocolVersion: "1",
    transport: "loopback-http",

    async snapshot() {
      await bootstrap();
      let saved = currentSessionId;
      if (!saved) {
        const fromUrl = new URL(location.href).searchParams.get("session");
        try { saved = fromUrl || localStorage.getItem(SESSION_KEY); } catch (_) { saved = fromUrl; }
      }
      if (saved) {
        // Do not silently create a fresh session after errors: doing so can
        // hide prior publishing receipts and bypass duplicate-send protection.
        return remember(await request(`/api/session?id=${encodeURIComponent(saved)}`, { method: "GET" }));
      }
      return remember(await request("/api/session/create", { json: {} }));
    },

    async newSession() {
      // Optional extension for portable hosts. Older native hosts may omit it.
      // The server rechecks the same condition; browser state is not authority.
      if (navigator.userActivation?.isActive !== true) {
        throw new HostError("USER_CLICK_REQUIRED", "请本人点击“新建素材批次”。");
      }
      if (!currentSessionId || currentSnapshot?.stage !== "FINISHED") {
        throw new HostError("PREVIOUS_BATCH_UNFINISHED", "只有上一批次明确完成后才能新建；不能跳过执行中、暂停或未知结果。");
      }
      return remember(await request("/api/session/create", { json: { previousSessionId: currentSessionId } }));
    },

    async selectKind({ sessionId: requestedId, kind, assetRevision }) {
      return remember(await request("/api/materials/kind", { json: {sessionId: sessionId(requestedId), kind, assetRevision} }));
    },

    async storeMedia({ sessionId: requestedId, kind, files }) {
      const id = sessionId(requestedId);
      const selected = Array.from(files || []);
      if (!["image_post", "video"].includes(kind) || selected.length < 1 || selected.length > 50) {
        throw new HostError("INVALID_MEDIA_BATCH", "请选择一组图片或一个主视频，每批最多 50 个文件。");
      }
      if (kind === "video" && selected.length !== 1) throw new HostError("ONE_VIDEO_REQUIRED", "一次会话仅接收一个主视频。");
      selected.forEach(assertFile);
      const ids = [];
      // Raw, sequential slices keep video memory bounded. No base64, parsing,
      // translation, OCR, metadata inference, or platform call occurs here.
      for (const file of selected) ids.push(await uploadFile(id, file, "media"));
      return remember(await request("/api/materials/media", { json: { sessionId: id, kind, uploadIds: ids } }));
    },

    async storeMetadata({ sessionId: requestedId, cover, coverAbsent, title, body, keepCoverRevision = null }) {
      const id = sessionId(requestedId);
      if (typeof title !== "string" || typeof body !== "string") throw new HostError("TEXT_REQUIRED", "请分别提供原样标题与正文。");
      if (cover && coverAbsent === true || keepCoverRevision !== null && (cover || coverAbsent === true || !Number.isInteger(keepCoverRevision))) throw new HostError("COVER_CONFLICT", "保留封面、新封面和无封面只能选择一种。");
      if (!cover && coverAbsent !== true && keepCoverRevision === null) throw new HostError("COVER_REQUIRED", "请保留原封面、上传新封面，或明确选择无封面。");
      const coverUploadId = cover ? await uploadFile(id, cover, "cover") : null;
      return remember(await request("/api/materials/metadata", { json: {
        sessionId: id, coverUploadId, coverAbsent: coverAbsent === true, title, body, keepCoverRevision,
      } }));
    },

    async confirmMaterials({ sessionId: requestedId, assetRevision, confirmed }) {
      if (confirmed !== true) throw new HostError("MATERIAL_CONFIRMATION_REQUIRED", "请先明确确认素材正确。");
      return remember(await request("/api/materials/confirm", { json: { sessionId: sessionId(requestedId), assetRevision, confirmed: true } }));
    },

    async preparePlan({ sessionId: requestedId, assetRevision, rows }) {
      return remember(await request("/api/plan/prepare", { json: { sessionId: sessionId(requestedId), assetRevision, rows } }));
    },

    async confirmPlan({ sessionId: requestedId, planId, planHash, confirmed }) {
      // Check synchronously, before the first await. A model-generated boolean
      // is not the trusted browser's final confirmation event.
      if (typeof confirmed !== "boolean") throw new HostError("CONFIRMATION_REQUIRED", "请选择是或否。");
      if (confirmed && navigator.userActivation?.isActive !== true) {
        throw new HostError("USER_CLICK_REQUIRED", "请由本人点击确认窗口中的“是”；不能由脚本代替最终确认。");
      }
      return remember(await request("/api/plan/confirm", { json: {
        sessionId: sessionId(requestedId), planId, planHash, confirmed,
      } }));
    },
  };

  Object.defineProperty(window, "OneClickPublishHost", { value: Object.freeze(host), writable: false, configurable: false });
})();
