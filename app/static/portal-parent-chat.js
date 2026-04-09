const payloadNode = document.getElementById("portal-parent-chat-data");

if (payloadNode) {
  const payload = JSON.parse(payloadNode.textContent || "{}");
  const thread = document.getElementById("parent-chat-thread");
  const form = document.getElementById("parent-chat-form");
  const input = document.getElementById("parent-chat-input");
  const submit = document.getElementById("parent-chat-submit");
  const status = document.getElementById("parent-chat-status");

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
    if (message.memory_saved) {
      const flags = document.createElement("div");
      flags.className = "parent-chat-bubble-flags";
      const chip = document.createElement("button");
      chip.className = "parent-chat-memory-chip";
      chip.type = "button";
      chip.dataset.parentChatMemoryToggle = "";
      chip.setAttribute("aria-expanded", "false");
      chip.textContent = message.memory_saved_label || "Saved to memory";
      flags.appendChild(chip);
      if ((message.memory_saved_details || []).length) {
        const popover = document.createElement("div");
        popover.className = "parent-chat-memory-popover";
        popover.dataset.parentChatMemoryPopover = "";
        popover.hidden = true;

        const title = document.createElement("p");
        title.className = "parent-chat-memory-popover-title";
        title.textContent = "Created memories";
        popover.appendChild(title);

        const list = document.createElement("ul");
        list.className = "parent-chat-memory-list";
        (message.memory_saved_details || []).forEach((detail) => {
          const item = document.createElement("li");
          item.className = "parent-chat-memory-list-item";

          const strong = document.createElement("strong");
          strong.textContent = detail.title || "Saved memory";
          item.appendChild(strong);

          const span = document.createElement("span");
          span.textContent = detail.content || "";
          item.appendChild(span);

          list.appendChild(item);
        });
        popover.appendChild(list);
        flags.appendChild(popover);
      }
      article.appendChild(flags);
    }
    thread.appendChild(article);
  };

  const closeMemoryPopovers = (exceptToggle = null) => {
    document.querySelectorAll("[data-parent-chat-memory-toggle]").forEach((toggle) => {
      const popover = toggle.parentElement?.querySelector("[data-parent-chat-memory-popover]");
      if (!popover || toggle === exceptToggle) return;
      toggle.setAttribute("aria-expanded", "false");
      popover.hidden = true;
    });
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
          }),
        });
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

  document.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-parent-chat-memory-toggle]");
    if (toggle) {
      const popover = toggle.parentElement?.querySelector("[data-parent-chat-memory-popover]");
      if (!popover) return;
      const isOpen = toggle.getAttribute("aria-expanded") === "true";
      closeMemoryPopovers(toggle);
      toggle.setAttribute("aria-expanded", isOpen ? "false" : "true");
      popover.hidden = isOpen;
      return;
    }
    if (!event.target.closest("[data-parent-chat-memory-popover]")) {
      closeMemoryPopovers();
    }
  });

  if (payload.status_message) {
    setStatus(payload.status_message, payload.status_tone || "muted");
  }

  scrollToBottom();
}
