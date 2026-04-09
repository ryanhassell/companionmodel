export const RequestFailureCode = Object.freeze({
  offline: "offline",
  timeout: "timeout",
  serverError: "server_error",
  authExpired: "auth_expired",
  redirected: "redirected",
  ok: "ok",
});

const DEFAULT_TIMEOUT_MS = 12000;
const SESSION_CHANNEL_NAME = "resona-portal-session";
const STORAGE_EVENT_KEY = "resona:portal:session-event";
const TAB_ID_KEY = "resona:portal:tab-id";

export const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

const readTabId = () => {
  try {
    const existing = window.sessionStorage.getItem(TAB_ID_KEY);
    if (existing) {
      return existing;
    }
    const next = `tab-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    window.sessionStorage.setItem(TAB_ID_KEY, next);
    return next;
  } catch {
    return `tab-${Date.now()}`;
  }
};

const TAB_ID = readTabId();

const listeners = new Set();
let broadcastChannel = null;

const ensureBroadcastChannel = () => {
  if (broadcastChannel || typeof window.BroadcastChannel !== "function") {
    return broadcastChannel;
  }
  broadcastChannel = new window.BroadcastChannel(SESSION_CHANNEL_NAME);
  broadcastChannel.addEventListener("message", (event) => {
    const payload = event?.data;
    if (!payload || payload.sourceId === TAB_ID) {
      return;
    }
    listeners.forEach((listener) => listener(payload));
  });
  return broadcastChannel;
};

window.addEventListener("storage", (event) => {
  if (event.key !== STORAGE_EVENT_KEY || !event.newValue) {
    return;
  }
  try {
    const payload = JSON.parse(event.newValue);
    if (!payload || payload.sourceId === TAB_ID) {
      return;
    }
    listeners.forEach((listener) => listener(payload));
  } catch {
    // Ignore malformed cross-tab payloads.
  }
});

export const subscribeSessionEvents = (listener) => {
  ensureBroadcastChannel();
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export const publishSessionEvent = (type, detail = {}) => {
  const payload = {
    type,
    detail,
    sourceId: TAB_ID,
    emittedAt: Date.now(),
  };
  const channel = ensureBroadcastChannel();
  if (channel) {
    channel.postMessage(payload);
  }
  try {
    window.localStorage.setItem(STORAGE_EVENT_KEY, JSON.stringify(payload));
    window.localStorage.removeItem(STORAGE_EVENT_KEY);
  } catch {
    // localStorage can be unavailable in privacy-constrained contexts.
  }
};

export const currentPathWithQuery = () => `${window.location.pathname}${window.location.search}`;

const normalizeHeaders = (headers, resumeUrl) => {
  const normalized = new Headers(headers || {});
  if (!normalized.has("Accept")) {
    normalized.set("Accept", "application/json");
  }
  if (!normalized.has("X-Resona-Request")) {
    normalized.set("X-Resona-Request", "fetch");
  }
  if (!normalized.has("X-Resona-Resume-Url")) {
    normalized.set("X-Resona-Resume-Url", resumeUrl || currentPathWithQuery());
  }
  return normalized;
};

const parseJsonSafely = async (response) => {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    return null;
  }
  try {
    return await response.json();
  } catch {
    return null;
  }
};

export const fetchJson = async (url, options = {}) => {
  const timeoutMs = Number(options.timeoutMs || DEFAULT_TIMEOUT_MS);
  const resumeUrl = options.resumeUrl || currentPathWithQuery();
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort("timeout"), timeoutMs);

  try {
    const response = await fetch(url, {
      ...options,
      headers: normalizeHeaders(options.headers, resumeUrl),
      signal: controller.signal,
    });
    const data = await parseJsonSafely(response);

    if (!data && response.redirected) {
      return {
        ok: false,
        code: RequestFailureCode.redirected,
        response,
        data: null,
        redirectUrl: response.url,
        retryable: false,
      };
    }

    if (!data && !response.ok) {
      return {
        ok: false,
        code: response.status === 401 || response.status === 403 ? RequestFailureCode.authExpired : RequestFailureCode.serverError,
        response,
        data: null,
        redirectUrl: null,
        retryable: response.status >= 500,
      };
    }

    if (!data && response.ok) {
      return {
        ok: false,
        code: RequestFailureCode.serverError,
        response,
        data: null,
        redirectUrl: null,
        retryable: false,
      };
    }

    if (!response.ok) {
      const code = data?.code || (response.status === 401 || response.status === 403 ? RequestFailureCode.authExpired : RequestFailureCode.serverError);
      return {
        ok: false,
        code,
        response,
        data,
        redirectUrl: data?.login_url || data?.resume_url || null,
        retryable: Boolean(data?.retryable ?? response.status >= 500),
      };
    }

    return {
      ok: true,
      code: RequestFailureCode.ok,
      response,
      data,
      redirectUrl: null,
      retryable: false,
    };
  } catch (error) {
    if (error?.name === "AbortError") {
      return {
        ok: false,
        code: RequestFailureCode.timeout,
        response: null,
        data: null,
        redirectUrl: null,
        retryable: true,
        error,
      };
    }
    const offline = window.navigator && window.navigator.onLine === false;
    return {
      ok: false,
      code: offline ? RequestFailureCode.offline : RequestFailureCode.serverError,
      response: null,
      data: null,
      redirectUrl: null,
      retryable: true,
      error,
    };
  } finally {
    window.clearTimeout(timeoutId);
  }
};

export const retryWithBackoff = async (operation, options = {}) => {
  const retries = Number(options.retries ?? 2);
  const baseDelayMs = Number(options.baseDelayMs ?? 900);
  const shouldRetry = options.shouldRetry || ((result) => Boolean(result?.retryable));
  const onRetry = options.onRetry || (() => {});

  let attempt = 0;
  let result = await operation();
  while (attempt < retries && shouldRetry(result)) {
    attempt += 1;
    onRetry(result, attempt);
    await wait(Math.min(baseDelayMs * 2 ** (attempt - 1), 4000));
    result = await operation();
  }
  return result;
};

export const watchConnectivity = ({ onOnline, onOffline, immediate = true } = {}) => {
  const onlineHandler = () => onOnline?.();
  const offlineHandler = () => onOffline?.();
  window.addEventListener("online", onlineHandler);
  window.addEventListener("offline", offlineHandler);
  if (immediate) {
    if (window.navigator && window.navigator.onLine === false) {
      onOffline?.();
    } else {
      onOnline?.();
    }
  }
  return () => {
    window.removeEventListener("online", onlineHandler);
    window.removeEventListener("offline", offlineHandler);
  };
};

export const createPortalBanner = (root = document.getElementById("portal-global-banner")) => {
  const titleNode = root?.querySelector("[data-portal-banner-title]");
  const messageNode = root?.querySelector("[data-portal-banner-message]");
  const actionsNode = root?.querySelector("[data-portal-banner-actions]");

  const hide = () => {
    if (!root) {
      return;
    }
    root.hidden = true;
    root.dataset.tone = "";
    if (actionsNode) {
      actionsNode.innerHTML = "";
    }
  };

  const show = ({ tone = "neutral", title = "", message = "", actions = [] } = {}) => {
    if (!root) {
      return;
    }
    root.hidden = false;
    root.dataset.tone = tone;
    if (titleNode) {
      titleNode.textContent = title;
    }
    if (messageNode) {
      messageNode.textContent = message;
    }
    if (actionsNode) {
      actionsNode.innerHTML = "";
      actions.forEach((action) => {
        const node = action.href ? document.createElement("a") : document.createElement("button");
        if (action.href) {
          node.href = action.href;
        } else {
          node.type = "button";
          node.addEventListener("click", () => action.onClick?.());
        }
        node.className = `button ${action.variant === "ghost" ? "ghost" : "secondary"}`.trim();
        node.textContent = action.label;
        actionsNode.appendChild(node);
      });
    }
  };

  return { show, hide };
};
