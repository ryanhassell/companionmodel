const payloadNode = document.getElementById("portal-parent-chat-data");

if (payloadNode) {
  const payload = JSON.parse(payloadNode.textContent || "{}");
  const thread = document.getElementById("parent-chat-thread");
  const form = document.getElementById("parent-chat-form");
  const input = document.getElementById("parent-chat-input");
  const submit = document.getElementById("parent-chat-submit");
  const status = document.getElementById("parent-chat-status");
  const threadIdInput = document.getElementById("parent-chat-thread-id");

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

  const renderMessage = (message) => {
    if (!thread) return;
    const article = document.createElement("article");
    article.className = `parent-chat-bubble ${message.sender === "assistant" ? "assistant" : "parent"}`;
    article.setAttribute("data-parent-chat-message", "");

    const meta = document.createElement("div");
    meta.className = "parent-chat-bubble-meta";
    const strong = document.createElement("strong");
    strong.textContent = message.sender === "assistant" ? "Resona" : "You";
    meta.appendChild(strong);
    const timestamp = formatTime(message.created_at);
    if (timestamp) {
      const span = document.createElement("span");
      span.dataset.parentChatTimestamp = message.created_at || "";
      span.textContent = timestamp;
      meta.appendChild(span);
    }

    const body = document.createElement("div");
    body.className = "parent-chat-bubble-body";
    body.textContent = message.body || "";

    article.appendChild(meta);
    article.appendChild(body);
    if ((message.memory_saved_details || []).length) {
      const log = document.createElement("div");
      log.className = "parent-chat-tool-log";
      (message.memory_saved_details || []).forEach((detail) => {
        const row = document.createElement("div");
        row.className = "parent-chat-tool-row";

        const badge = document.createElement("span");
        badge.className = "parent-chat-tool-badge";
        badge.textContent = "Tool";
        row.appendChild(badge);

        const label = document.createElement("span");
        label.textContent = `Added Memory: ${detail.title || "Saved memory"}`;
        row.appendChild(label);

        log.appendChild(row);
      });
      article.appendChild(log);
    }
    thread.appendChild(article);
  };

  document.querySelectorAll("[data-parent-chat-timestamp]").forEach((node) => {
    const rawValue = node.dataset.parentChatTimestamp || "";
    const formatted = formatTime(rawValue);
    node.textContent = formatted || "";
  });

  const setStatus = (message, tone = "muted") => {
    if (!status) return;
    status.textContent = message || "";
    status.dataset.tone = tone;
  };

  const scrollToBottom = () => {
    if (!thread) return;
    thread.scrollTop = thread.scrollHeight;
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

  if (form && input && submit) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message) {
        setStatus("Write a message before sending.", "warning");
        return;
      }
      submit.disabled = true;
      input.disabled = true;
      setStatus("Thinking through that...", "info");
      try {
        const response = await requestJson(payload.send_url, {
          method: "POST",
          body: JSON.stringify({
            csrf_token: payload.csrf_token,
            message,
            thread_id: threadIdInput?.value || payload.thread_id || "",
          }),
        });
        if (threadIdInput && response.thread_id) {
          threadIdInput.value = response.thread_id;
        }
        (response.messages || []).forEach((item) => renderMessage(item));
        input.value = "";
        setStatus("Sent and added to memory.", "success");
        scrollToBottom();
      } catch (error) {
        setStatus(error.message || "We couldn't send that right now.", "danger");
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
