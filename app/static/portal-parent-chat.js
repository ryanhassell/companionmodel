const payloadNode = document.getElementById("portal-parent-chat-data");

if (payloadNode) {
  const payload = JSON.parse(payloadNode.textContent || "{}");
  const assistantLabel = payload.assistant_label || "Resona";
  const thread = document.getElementById("parent-chat-thread");
  const form = document.getElementById("parent-chat-form");
  const input = document.getElementById("parent-chat-input");
  const submit = document.getElementById("parent-chat-submit");
  const status = document.getElementById("parent-chat-status");
  const threadIdInput = document.getElementById("parent-chat-thread-id");
  const contextInput = document.getElementById("parent-chat-context");

  let liveAssistant = null;

  const formatTime = (value) => {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  };

  const escapeHtml = (value) =>
    String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");

  const setStatus = (message, tone = "muted") => {
    if (!status) return;
    status.textContent = message || "";
    status.dataset.tone = tone;
  };

  const scrollToBottom = () => {
    if (!thread) return;
    thread.scrollTop = thread.scrollHeight;
  };

  const clearEmptyPlaceholder = () => {
    if (!thread) return;
    thread.querySelectorAll("[data-parent-chat-empty]").forEach((node) => node.remove());
  };

  const requestJson = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Resona-Request": "fetch",
        ...(options.headers || {}),
      },
    });
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await response.json() : {};
    if (!response.ok || data.ok === false) {
      if (data.login_url) {
        window.location.href = data.login_url;
        throw new Error("Your session expired.");
      }
      throw new Error(data.detail || "Unexpected response from server");
    }
    return data;
  };

  const createBubble = (sender, createdAt = null) => {
    const article = document.createElement("article");
    article.className = `parent-chat-bubble ${sender === "assistant" ? "assistant" : "parent"}`;
    article.setAttribute("data-parent-chat-message", "");

    const meta = document.createElement("div");
    meta.className = "parent-chat-bubble-meta";
    const strong = document.createElement("strong");
    strong.textContent = sender === "assistant" ? assistantLabel : "You";
    meta.appendChild(strong);
    const span = document.createElement("span");
    if (createdAt) {
      span.dataset.parentChatTimestamp = createdAt;
      span.textContent = formatTime(createdAt);
    }
    meta.appendChild(span);

    const body = document.createElement("div");
    body.className = "parent-chat-bubble-body";

    const activity = document.createElement("div");
    activity.className = "parent-chat-memory-log";
    activity.hidden = true;

    article.appendChild(meta);
    article.appendChild(body);
    article.appendChild(activity);
    return { article, meta, body, activity, timestamp: span, activityEvents: [] };
  };

  const memoryHrefForId = (memoryId) => {
    if (!memoryId) return "";
    const base = payload.memory_map_url || "/app/memories/map";
    return `${base}?node=${encodeURIComponent(memoryId)}`;
  };

  const renderActivityEvent = (event) => {
    const details = Array.isArray(event?.details) ? event.details : [];
    const href = event?.href || (event?.memory_id ? memoryHrefForId(event.memory_id) : "");
    if (details.length) {
      const detailMarkup = details
        .map((detail) => {
          const detailHref = detail?.id ? memoryHrefForId(detail.id) : "";
          if (detailHref) {
            return `<a class="parent-chat-memory-detail" href="${escapeHtml(detailHref)}">${escapeHtml(detail?.title || "Saved memory")}</a>`;
          }
          return `<span class="parent-chat-memory-detail">${escapeHtml(detail?.title || "Saved memory")}</span>`;
        })
        .join("");
      const summary = href
        ? `<a href="${escapeHtml(href)}">${escapeHtml(event?.label || "Updated memory")}</a>`
        : `<span>${escapeHtml(event?.label || "Updated memory")}</span>`;
      return `
        <details class="parent-chat-memory-event">
          <summary class="parent-chat-memory-row">${summary}</summary>
          <div class="parent-chat-memory-detail-list">${detailMarkup}</div>
        </details>
      `;
    }
    const content = href
      ? `<a href="${escapeHtml(href)}">${escapeHtml(event?.label || event?.detail || "Updated memory")}</a>`
      : `<span>${escapeHtml(event?.label || event?.detail || "Updated memory")}</span>`;
    return `<div class="parent-chat-memory-row">${content}</div>`;
  };

  const renderActivityEvents = (container, events) => {
    if (!container) return;
    const rows = Array.isArray(events) ? events : [];
    container.innerHTML = "";
    if (!rows.length) {
      container.hidden = true;
      return;
    }
    container.innerHTML = rows.map((event) => renderActivityEvent(event)).join("");
    container.hidden = false;
  };

  const appendMessage = (message) => {
    if (!thread || !message) return null;
    clearEmptyPlaceholder();
    const bubble = createBubble(message.sender, message.created_at || null);
    bubble.article.dataset.messageId = message.id || "";
    bubble.article.dataset.messageKind = message.kind || "message";
    bubble.body.textContent = message.body || "";
    if (message.sender === "assistant") {
      renderActivityEvents(bubble.activity, message.activity_events || []);
    }
    thread.appendChild(bubble.article);
    scrollToBottom();
    return bubble.article;
  };

  const startLiveAssistant = () => {
    if (!thread) return null;
    if (liveAssistant?.article?.isConnected) return liveAssistant;
    clearEmptyPlaceholder();
    const bubble = createBubble("assistant", new Date().toISOString());
    bubble.article.dataset.liveAssistant = "1";
    thread.appendChild(bubble.article);
    liveAssistant = bubble;
    scrollToBottom();
    return bubble;
  };

  const appendLiveDelta = (text) => {
    const bubble = startLiveAssistant();
    if (!bubble || !text) return;
    bubble.body.textContent += text;
    scrollToBottom();
  };

  const appendLiveActivity = (event) => {
    const bubble = startLiveAssistant();
    if (!bubble) return;
    bubble.activityEvents.push(event);
    renderActivityEvents(bubble.activity, bubble.activityEvents);
    scrollToBottom();
  };

  const finishLiveAssistant = (message) => {
    if (!thread || !message) return;
    if (liveAssistant?.article?.isConnected) {
      const replacement = appendMessage(message);
      if (replacement && liveAssistant.article.isConnected) {
        liveAssistant.article.replaceWith(replacement);
      }
      liveAssistant = null;
      return;
    }
    appendMessage(message);
  };

  const parseSseChunk = (raw) => {
    const blocks = raw.split("\n\n");
    return blocks
      .map((block) => block.trim())
      .filter(Boolean)
      .map((block) => {
        const lines = block.split("\n");
        let eventType = "message";
        const dataLines = [];
        lines.forEach((line) => {
          if (line.startsWith("event:")) {
            eventType = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trim());
          }
        });
        if (!dataLines.length) return null;
        try {
          return { eventType, payload: JSON.parse(dataLines.join("\n")) };
        } catch {
          return null;
        }
      })
      .filter(Boolean);
  };

  const handleStreamEvent = (eventType, event) => {
    if (eventType === "thread_ready") {
      if (threadIdInput && event.thread_id) {
        threadIdInput.value = event.thread_id;
      }
      if (event.thread_id) {
        const url = new URL(window.location.href);
        url.searchParams.set("thread", event.thread_id);
        window.history.replaceState({}, "", url.toString());
      }
      return;
    }
    if (eventType === "status") {
      setStatus(event.label || event.detail || `${assistantLabel} is thinking...`, "info");
      return;
    }
    if (eventType === "assistant_delta") {
      appendLiveDelta(event.text || "");
      return;
    }
    if (["memory_added", "memory_batch_added", "memory_updated", "memory_linked", "memory_refactored", "activity"].includes(eventType)) {
      appendLiveActivity(event);
      return;
    }
    if (eventType === "assistant_message") {
      if (threadIdInput && event.thread_id) {
        threadIdInput.value = event.thread_id;
      }
      finishLiveAssistant(event.message);
      setStatus("Sent.", "success");
      return;
    }
    if (eventType === "run_complete") {
      setStatus("Sent.", "success");
      return;
    }
    if (eventType === "run_error") {
      setStatus(event.detail || "We couldn't send that right now.", "danger");
    }
  };

  const streamSend = async (message) => {
    const response = await fetch(payload.stream_url || payload.send_url, {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-Resona-Request": "fetch",
      },
      body: JSON.stringify({
        csrf_token: payload.csrf_token,
        message,
        thread_id: threadIdInput?.value || payload.thread_id || "",
        question_context: contextInput?.value || "",
      }),
    });

    if (!response.ok) {
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        const data = await response.json();
        if (data.login_url) {
          window.location.href = data.login_url;
          throw new Error("Your session expired.");
        }
        throw new Error(data.detail || "Unexpected response from server");
      }
      throw new Error("Unexpected response from server");
    }

    if (!response.body) {
      throw new Error("Streaming is unavailable in this browser.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const boundary = buffer.lastIndexOf("\n\n");
      if (boundary !== -1) {
        const complete = buffer.slice(0, boundary + 2);
        buffer = buffer.slice(boundary + 2);
        parseSseChunk(complete).forEach((item) => {
          handleStreamEvent(item.eventType, item.payload);
        });
      }
      if (done) {
        if (buffer.trim()) {
          parseSseChunk(buffer).forEach((item) => {
            handleStreamEvent(item.eventType, item.payload);
          });
        }
        break;
      }
    }
  };

  const fallbackSend = async (message) => {
    const response = await requestJson(payload.send_url, {
      method: "POST",
      body: JSON.stringify({
        csrf_token: payload.csrf_token,
        message,
        thread_id: threadIdInput?.value || payload.thread_id || "",
        question_context: contextInput?.value || "",
      }),
    });
    if (threadIdInput && response.thread_id) {
      threadIdInput.value = response.thread_id;
    }
    (response.messages || []).forEach((item) => appendMessage(item));
    setStatus("Sent.", "success");
    scrollToBottom();
  };

  document.querySelectorAll("[data-parent-chat-timestamp]").forEach((node) => {
    const rawValue = node.dataset.parentChatTimestamp || "";
    node.textContent = formatTime(rawValue) || "";
  });

  if (form && input && submit) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message) {
        setStatus("Write a message before sending.", "warning");
        return;
      }
      const parentBubble = appendMessage({
        id: `local-parent-${Date.now()}`,
        sender: "parent",
        body: message,
        created_at: new Date().toISOString(),
        kind: "message",
      });
      submit.disabled = true;
      input.disabled = true;
      input.value = "";
      liveAssistant = null;
      setStatus(`${assistantLabel} is thinking...`, "info");
      try {
        await streamSend(message);
      } catch (error) {
        if (parentBubble?.isConnected) {
          parentBubble.remove();
        }
        if (liveAssistant?.article?.isConnected) {
          liveAssistant.article.remove();
          liveAssistant = null;
        }
        try {
          await fallbackSend(message);
        } catch (fallbackError) {
          setStatus(fallbackError.message || error.message || "We couldn't send that right now.", "danger");
          input.value = message;
        }
      } finally {
        submit.disabled = false;
        input.disabled = false;
        input.focus();
      }
    });
  }

  if (payload.status_message) {
    setStatus(payload.status_message, payload.status_tone || "muted");
  }

  scrollToBottom();
}
