const payloadNode = document.getElementById("portal-memories-data");

if (payloadNode) {
  const payload = JSON.parse(payloadNode.textContent || "{}");
  const graphElement = document.getElementById("memory-graph");
  const graphEmpty = document.getElementById("memory-graph-empty");
  const showArchivedToggle = document.getElementById("memory-show-archived");
  const inspectorEmpty = document.getElementById("memory-inspector-empty");
  const inspectorBody = document.getElementById("memory-inspector-body");
  const inspectorStatus = document.getElementById("memory-inspector-status");
  const inspectorTitle = document.getElementById("memory-inspector-title");
  const inspectorMeta = document.getElementById("memory-inspector-meta");
  const linkedList = document.getElementById("memory-linked-list");
  const deletePreview = document.getElementById("memory-delete-preview");
  const deletePreviewList = document.getElementById("memory-delete-preview-list");
  const saveForm = document.getElementById("memory-inspector-form");
  const saveButton = document.getElementById("memory-save-button");
  const previewDeleteButton = document.getElementById("memory-preview-delete-button");
  const confirmDeleteButton = document.getElementById("memory-confirm-delete-button");
  const cancelDeleteButton = document.getElementById("memory-cancel-delete-button");
  const fieldTitle = document.getElementById("memory-field-title");
  const fieldContent = document.getElementById("memory-field-content");
  const fieldSummary = document.getElementById("memory-field-summary");
  const fieldTags = document.getElementById("memory-field-tags");
  const fieldPinned = document.getElementById("memory-field-pinned");
  const fieldArchived = document.getElementById("memory-field-archived");

  let selectedMemoryId = null;
  let graph = null;
  let latestGraph = { nodes: [], structural_edges: [], similarity_edges: [] };

  const requestJson = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await response.json() : {};
    if (!response.ok || data.ok === false) {
      if (data.login_url) {
        window.location.href = data.login_url;
        throw new Error("Session expired");
      }
      throw new Error(data.detail || data.message || "Unexpected response from server");
    }
    return data;
  };

  const setStatus = (message, tone = "info") => {
    if (!inspectorStatus) return;
    if (!message) {
      inspectorStatus.hidden = true;
      inspectorStatus.textContent = "";
      inspectorStatus.removeAttribute("data-tone");
      return;
    }
    inspectorStatus.hidden = false;
    inspectorStatus.textContent = message;
    inspectorStatus.dataset.tone = tone;
  };

  const clearDeletePreview = () => {
    if (!deletePreview) return;
    deletePreview.hidden = true;
    if (deletePreviewList) {
      deletePreviewList.innerHTML = "";
    }
  };

  const renderLinkedMemories = (items) => {
    if (!linkedList) return;
    if (!items.length) {
      linkedList.innerHTML = '<p class="subtle">No direct links are available for this memory yet.</p>';
      return;
    }
    linkedList.innerHTML = items
      .map(
        (item) => `
          <button type="button" class="memory-linked-item" data-linked-memory-id="${item.id}">
            <div>
              <strong>${escapeHtml(item.title)}</strong>
              <p>${escapeHtml(item.relationship_label)}</p>
              <small>${escapeHtml(item.summary || "")}</small>
            </div>
            <span class="pill ${item.kind === "similarity" ? "memory-pill-similarity" : ""}">${item.kind === "similarity" ? "Similar" : "Linked"}</span>
          </button>
        `,
      )
      .join("");
    linkedList.querySelectorAll("[data-linked-memory-id]").forEach((button) => {
      button.addEventListener("click", () => {
        void loadMemory(button.dataset.linkedMemoryId || "");
      });
    });
  };

  const fillInspector = (memory) => {
    selectedMemoryId = memory.id;
    if (inspectorEmpty) inspectorEmpty.hidden = true;
    if (inspectorBody) inspectorBody.hidden = false;
    if (inspectorTitle) inspectorTitle.textContent = memory.title;
    if (inspectorMeta) {
      const bits = [memory.memory_type_label];
      if (memory.updated_at) bits.push(`Updated ${formatDateTime(memory.updated_at)}`);
      if (memory.pinned) bits.push("Pinned");
      if (memory.archived) bits.push("Archived");
      inspectorMeta.textContent = bits.join(" · ");
    }
    if (fieldTitle) fieldTitle.value = memory.title || "";
    if (fieldContent) fieldContent.value = memory.content || "";
    if (fieldSummary) fieldSummary.value = memory.summary || "";
    if (fieldTags) fieldTags.value = Array.isArray(memory.tags) ? memory.tags.join(", ") : "";
    if (fieldPinned) fieldPinned.checked = Boolean(memory.pinned);
    if (fieldArchived) fieldArchived.checked = Boolean(memory.archived);
    renderLinkedMemories(Array.isArray(memory.linked_memories) ? memory.linked_memories : []);
    clearDeletePreview();
    highlightSelectedMemory(memory.id);
  };

  const loadMemory = async (memoryId) => {
    if (!memoryId) return;
    setStatus("Loading memory...", "info");
    try {
      const response = await requestJson(`${payload.detail_base_url}/${memoryId}`);
      fillInspector(response.memory);
      setStatus("", "info");
    } catch (error) {
      setStatus(error.message || "We couldn't load that memory right now.", "danger");
    }
  };

  const previewDelete = async () => {
    if (!selectedMemoryId) return;
    setStatus("Checking what would be removed...", "warning");
    try {
      const response = await requestJson(`${payload.detail_base_url}/${selectedMemoryId}/delete-preview`, {
        method: "POST",
        body: JSON.stringify({ csrf_token: payload.csrf_token }),
      });
      const preview = response.preview;
      if (deletePreviewList) {
        deletePreviewList.innerHTML = preview.affected
          .map(
            (item) => `
              <div class="memory-delete-preview-item">
                <strong>${escapeHtml(item.title)}</strong>
                <p>${escapeHtml(item.reason)}</p>
              </div>
            `,
          )
          .join("");
      }
      if (deletePreview) deletePreview.hidden = false;
      setStatus(`${preview.deleted_count} memory${preview.deleted_count === 1 ? "" : "ies"} would be removed.`, "warning");
    } catch (error) {
      setStatus(error.message || "We couldn't build the delete preview right now.", "danger");
    }
  };

  const performDelete = async () => {
    if (!selectedMemoryId) return;
    setStatus("Deleting memory...", "warning");
    try {
      const response = await requestJson(`${payload.detail_base_url}/${selectedMemoryId}/delete`, {
        method: "POST",
        body: JSON.stringify({ csrf_token: payload.csrf_token }),
      });
      const deletedIds = new Set(response.deleted_ids || []);
      selectedMemoryId = null;
      if (inspectorEmpty) inspectorEmpty.hidden = false;
      if (inspectorBody) inspectorBody.hidden = true;
      document.querySelectorAll("[data-memory-id]").forEach((button) => {
        if (deletedIds.has(button.dataset.memoryId || "")) {
          button.remove();
        }
      });
      clearDeletePreview();
      setStatus(`Removed ${response.preview.deleted_count} memory${response.preview.deleted_count === 1 ? "" : "ies"}.`, "success");
      await refreshGraph();
    } catch (error) {
      setStatus(error.message || "We couldn't delete that memory right now.", "danger");
    }
  };

  const saveMemory = async (event) => {
    event.preventDefault();
    if (!selectedMemoryId) return;
    if (saveButton) saveButton.disabled = true;
    setStatus("Saving changes...", "info");
    try {
      const response = await requestJson(`${payload.detail_base_url}/${selectedMemoryId}`, {
        method: "POST",
        body: JSON.stringify({
          csrf_token: payload.csrf_token,
          data: {
            title: fieldTitle ? fieldTitle.value : "",
            content: fieldContent ? fieldContent.value : "",
            summary: fieldSummary ? fieldSummary.value : "",
            tags: fieldTags ? fieldTags.value : "",
            pinned: fieldPinned ? fieldPinned.checked : false,
            archived: fieldArchived ? fieldArchived.checked : false,
          },
        }),
      });
      fillInspector(response.memory);
      setStatus("Memory updated.", "success");
      syncLibraryCard(response.memory);
      await refreshGraph();
    } catch (error) {
      setStatus(error.message || "We couldn't save those changes.", "danger");
    } finally {
      if (saveButton) saveButton.disabled = false;
    }
  };

  const syncLibraryCard = (memory) => {
    const button = document.querySelector(`[data-memory-id="${memory.id}"]`);
    if (!button) return;
    const title = button.querySelector("strong");
    const summary = button.querySelector("p:not(.subtle)");
    const meta = button.querySelector(".subtle");
    if (title) title.textContent = memory.title;
    if (summary) summary.textContent = memory.summary || memory.content;
    if (meta) meta.textContent = `${memory.memory_type_label} · ${memory.updated_at ? `Updated ${formatDateTime(memory.updated_at)}` : "Recently updated"}`;
  };

  const refreshGraph = async () => {
    if (!graphElement) return;
    try {
      const data = await requestJson(`${payload.graph_url}${showArchivedToggle && showArchivedToggle.checked ? "?archived=1" : ""}`);
      latestGraph = data;
      renderGraph(data);
    } catch (error) {
      if (graphEmpty) {
        graphEmpty.hidden = false;
        graphEmpty.querySelector("p").textContent = error.message || "The memory graph is unavailable right now.";
      }
    }
  };

  const renderGraph = (data) => {
    if (!graphElement || typeof window.cytoscape === "undefined") return;
    const nodes = Array.isArray(data.nodes) ? data.nodes : [];
    const structuralEdges = Array.isArray(data.structural_edges) ? data.structural_edges : [];
    const similarityEdges = Array.isArray(data.similarity_edges) ? data.similarity_edges : [];
    if (!nodes.length) {
      graphElement.innerHTML = "";
      if (graph) {
        graph.destroy();
        graph = null;
      }
      if (graphEmpty) graphEmpty.hidden = false;
      return;
    }
    if (graphEmpty) graphEmpty.hidden = true;
    const elements = [
      ...nodes.map((node) => ({
        data: {
          id: node.id,
          label: node.label,
          summary: node.summary,
          memoryType: node.memory_type,
        },
        classes: [node.pinned ? "is-pinned" : "", node.archived ? "is-archived" : ""].join(" ").trim(),
      })),
      ...structuralEdges.map((edge) => ({
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: edge.label || "",
        },
        classes: `is-structural ${edge.relationship_type || ""}`,
      })),
      ...similarityEdges.map((edge) => ({
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: edge.label || "",
        },
        classes: "is-similarity",
      })),
    ];

    if (graph) {
      graph.destroy();
    }

    graph = window.cytoscape({
      container: graphElement,
      elements,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#fcfaf5",
            "border-width": 2,
            "border-color": "#1e5f53",
            label: "data(label)",
            color: "#222827",
            "font-size": 11,
            "text-wrap": "wrap",
            "text-max-width": 86,
            "text-valign": "center",
            "text-halign": "center",
            width: 58,
            height: 58,
            padding: 8,
          },
        },
        {
          selector: "node.is-pinned",
          style: {
            "border-color": "#76684f",
            "border-width": 3,
          },
        },
        {
          selector: "node.is-archived",
          style: {
            opacity: 0.56,
            "background-color": "#f1ebe1",
          },
        },
        {
          selector: "node.is-selected",
          style: {
            "background-color": "#eaf2ef",
            "border-color": "#1e5f53",
            "border-width": 4,
          },
        },
        {
          selector: "edge",
          style: {
            width: 2,
            "line-color": "#cfc7bb",
            "target-arrow-color": "#cfc7bb",
            "curve-style": "bezier",
            opacity: 0.9,
          },
        },
        {
          selector: "edge.is-structural",
          style: {
            width: 2.5,
            "line-color": "#76684f",
          },
        },
        {
          selector: "edge.is-similarity",
          style: {
            width: 1.5,
            "line-style": "dashed",
            "line-color": "#9ea7a4",
            opacity: 0.72,
          },
        },
      ],
      layout: {
        name: "cose",
        animate: true,
        animationDuration: 360,
        fit: true,
        padding: 28,
        nodeRepulsion: 9000,
        idealEdgeLength: 110,
      },
    });

    graph.on("tap", "node", (event) => {
      void loadMemory(event.target.id());
    });
    highlightSelectedMemory(selectedMemoryId);
  };

  const highlightSelectedMemory = (memoryId) => {
    if (!graph) return;
    graph.nodes().removeClass("is-selected");
    if (!memoryId) return;
    const node = graph.$id(memoryId);
    if (node) node.addClass("is-selected");
  };

  if (saveForm) {
    saveForm.addEventListener("submit", (event) => {
      void saveMemory(event);
    });
  }
  if (previewDeleteButton) {
    previewDeleteButton.addEventListener("click", () => {
      void previewDelete();
    });
  }
  if (confirmDeleteButton) {
    confirmDeleteButton.addEventListener("click", () => {
      void performDelete();
    });
  }
  if (cancelDeleteButton) {
    cancelDeleteButton.addEventListener("click", () => {
      clearDeletePreview();
      setStatus("", "info");
    });
  }
  if (showArchivedToggle) {
    showArchivedToggle.addEventListener("change", () => {
      const nextUrl = new URL(window.location.href);
      if (showArchivedToggle.checked) {
        nextUrl.searchParams.set("archived", "1");
      } else {
        nextUrl.searchParams.delete("archived");
      }
      window.history.replaceState({}, "", nextUrl);
      void refreshGraph();
    });
  }
  document.querySelectorAll("[data-memory-id]").forEach((button) => {
    button.addEventListener("click", () => {
      void loadMemory(button.dataset.memoryId || "");
    });
  });

  if (graphElement) {
    void refreshGraph();
  }
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
