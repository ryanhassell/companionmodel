const payloadNode = document.getElementById("portal-memories-data");

if (payloadNode) {
  const payload = JSON.parse(payloadNode.textContent || "{}");
  const graphElement = document.getElementById("memory-graph");
  const graphView = String(payload.view || graphElement?.dataset.view || "map");
  const graphEmpty = document.getElementById("memory-graph-empty");
  const recentList = document.getElementById("memory-recent-list");
  const recentEmpty = document.getElementById("memory-recent-empty");
  const recentPrevButton = document.getElementById("memory-recent-prev-button");
  const recentNextButton = document.getElementById("memory-recent-next-button");
  const recentPageLabel = document.getElementById("memory-recent-page-label");
  const showArchivedToggle = document.getElementById("memory-show-archived");
  const showSimilarityToggle = document.getElementById("memory-show-similarity");
  const searchInput = document.getElementById("memory-search-input");
  const searchSubmit = document.getElementById("memory-search-submit");
  const searchSuggestions = document.getElementById("memory-search-suggestions");
  const fitButton = document.getElementById("memory-fit-button");
  const centerChildButton = document.getElementById("memory-center-child-button");
  const clearFocusButton = document.getElementById("memory-clear-focus-button");
  const legendToggle = document.getElementById("memory-legend-toggle");
  const legendPanel = document.getElementById("memory-legend-panel");

  const inspectorEmpty = document.getElementById("memory-inspector-empty");
  const inspectorNode = document.getElementById("memory-inspector-node");
  const inspectorBody = document.getElementById("memory-inspector-body");
  const inspectorStatus = document.getElementById("memory-inspector-status");

  const nodeBreadcrumbs = document.getElementById("memory-node-breadcrumbs");
  const nodeIconWrap = document.getElementById("memory-node-icon-wrap");
  const nodeTitle = document.getElementById("memory-node-title");
  const nodeMeta = document.getElementById("memory-node-meta");
  const nodeSummary = document.getElementById("memory-node-summary");
  const nodeMemoryCount = document.getElementById("memory-node-memory-count");
  const nodeBranchCount = document.getElementById("memory-node-branch-count");
  const nodeQuickLinks = document.getElementById("memory-node-quick-links");
  const nodeRelatedList = document.getElementById("memory-node-related-list");
  const openBranchLink = document.getElementById("memory-open-branch-link");

  const memoryBreadcrumbs = document.getElementById("memory-memory-breadcrumbs");
  const inspectorTitle = document.getElementById("memory-inspector-title");
  const inspectorMeta = document.getElementById("memory-inspector-meta");
  const fitSummary = document.getElementById("memory-fit-summary");
  const fitChips = document.getElementById("memory-fit-chips");
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

  let selectedNodeId = String(payload.selected_node_id || "").trim() || null;
  let selectedMemoryId = null;
  let hoveredNodeId = null;
  let graph = null;
  let latestGraph = { nodes: [], structural_edges: [], similarity_edges: [] };
  let recentPage = Number(payload.recent_page || 1) || 1;
  let recentPageTotal = Number(payload.recent_page_total || 1) || 1;

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

  const bindMemoryButtons = (scope = document) => {
    scope.querySelectorAll("[data-memory-id]").forEach((button) => {
      if (button.dataset.boundMemoryClick === "true") return;
      button.dataset.boundMemoryClick = "true";
      button.addEventListener("click", () => {
        void loadMemory(button.dataset.memoryId || "", { center: true });
      });
    });
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

  const nodeIndex = () => new Map((Array.isArray(latestGraph.nodes) ? latestGraph.nodes : []).map((node) => [node.id, node]));

  const structuralEdges = () => (Array.isArray(latestGraph.structural_edges) ? latestGraph.structural_edges : []);

  const currentUrl = () => new URL(window.location.href);

  const syncBrowserQuery = () => {
    const url = currentUrl();
    const search = String(searchInput?.value || "").trim();
    if (search) {
      url.searchParams.set("q", search);
    } else {
      url.searchParams.delete("q");
    }
    if (selectedNodeId) {
      url.searchParams.set("node", selectedNodeId);
    } else {
      url.searchParams.delete("node");
    }
    if (showArchivedToggle?.checked) {
      url.searchParams.set("archived", "1");
    } else {
      url.searchParams.delete("archived");
    }
    if (showSimilarityToggle && !showSimilarityToggle.checked) {
      url.searchParams.set("similar", "0");
    } else {
      url.searchParams.delete("similar");
    }
    window.history.replaceState({}, "", url.toString());
  };

  const graphRequestUrl = () => {
    const url = new URL(payload.graph_url || "/app/memories/graph-data", window.location.origin);
    const current = currentUrl();
    ["q", "node", "branch"].forEach((name) => {
      const value = current.searchParams.get(name);
      if (value) {
        url.searchParams.set(name, value);
      }
    });
    if (showArchivedToggle?.checked) {
      url.searchParams.set("archived", "1");
    }
    if (showSimilarityToggle && !showSimilarityToggle.checked) {
      url.searchParams.set("similar", "0");
    }
    return `${url.pathname}${url.search}`;
  };

  const recentRequestUrl = (page) => {
    const url = new URL(payload.recent_list_url || "/app/memories/recent-list", window.location.origin);
    url.searchParams.set("view", graphView);
    url.searchParams.set("page", String(Math.max(page || 1, 1)));
    if (showArchivedToggle?.checked) {
      url.searchParams.set("archived", "1");
    }
    return `${url.pathname}${url.search}`;
  };

  const memoryLibraryHrefForNode = (nodeId) => {
    const url = new URL(payload.library_url || "/app/memories/library", window.location.origin);
    if (nodeId) {
      url.searchParams.set("branch", nodeId);
    }
    if (showArchivedToggle?.checked) {
      url.searchParams.set("archived", "1");
    }
    const search = String(searchInput?.value || "").trim();
    if (search) {
      url.searchParams.set("q", search);
    }
    return `${url.pathname}${url.search}`;
  };

  const memoryMapHrefForId = (memoryId) => {
    if (!memoryId) return "";
    const base = payload.map_url || "/app/memories/map";
    const url = new URL(base, window.location.origin);
    url.searchParams.set("node", memoryId);
    if (showArchivedToggle?.checked) {
      url.searchParams.set("archived", "1");
    }
    if (showSimilarityToggle && !showSimilarityToggle.checked) {
      url.searchParams.set("similar", "0");
    }
    return `${url.pathname}${url.search}`;
  };

  const activeFocusNodeId = () => hoveredNodeId || selectedNodeId;

  const clearDeletePreview = () => {
    if (!deletePreview) return;
    deletePreview.hidden = true;
    if (deletePreviewList) {
      deletePreviewList.innerHTML = "";
    }
  };

  const showEmptyInspector = () => {
    if (inspectorEmpty) inspectorEmpty.hidden = false;
    if (inspectorNode) inspectorNode.hidden = true;
    if (inspectorBody) inspectorBody.hidden = true;
  };

  const renderBreadcrumbs = (container, items) => {
    if (!container) return;
    const rows = Array.isArray(items) ? items : [];
    container.innerHTML = "";
    if (!rows.length) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const fragment = document.createDocumentFragment();
    rows.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "memory-breadcrumb";
      button.textContent = item?.label || "Node";
      if (item?.id) {
        button.dataset.nodeTarget = item.id;
      } else {
        button.disabled = true;
      }
      fragment.appendChild(button);
      if (index < rows.length - 1) {
        const separator = document.createElement("span");
        separator.className = "memory-breadcrumb-separator";
        separator.textContent = "/";
        fragment.appendChild(separator);
      }
    });
    container.appendChild(fragment);
    container.querySelectorAll("[data-node-target]").forEach((button) => {
      button.addEventListener("click", () => {
        const nodeId = button.dataset.nodeTarget || "";
        if (!nodeId) return;
        const targetNode = nodeIndex().get(nodeId);
        if (!targetNode) return;
        if (targetNode.kind === "memory") {
          void loadMemory(nodeId, { center: true });
          return;
        }
        showNodeInspector(targetNode, { center: true });
      });
    });
  };

  const ICONS = {
    child:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.25"></circle><path d="M5.5 19c1.4-3.1 4-4.7 6.5-4.7s5.1 1.6 6.5 4.7"></path></svg>',
    family:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="9" r="2.5"></circle><circle cx="16" cy="9" r="2.5"></circle><path d="M3.5 18c.9-2.2 2.7-3.7 4.5-3.7 1.2 0 2.3.5 3.2 1.5"></path><path d="M12.3 15.8c.9-.9 2-1.5 3.2-1.5 1.8 0 3.6 1.5 4.5 3.7"></path></svg>',
    friend:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8.5" r="2.75"></circle><circle cx="15.5" cy="10.5" r="2.25"></circle><path d="M4.5 18.5c1-2.7 3-4.3 5.4-4.3 1.9 0 3.6 1 4.7 2.8"></path></svg>',
    pet:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="8" r="1.7"></circle><circle cx="15.8" cy="8" r="1.7"></circle><circle cx="6.2" cy="12.1" r="1.5"></circle><circle cx="17.8" cy="12.1" r="1.5"></circle><path d="M9 15.2c.8-.8 1.9-1.2 3-1.2s2.2.4 3 1.2c.4.4.4 1 0 1.4-.7.8-1.8 1.2-3 1.2s-2.3-.4-3-1.2a1 1 0 0 1 0-1.4Z"></path></svg>',
    artist:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10"></path><path d="M12 6.5 18.5 5v8"></path><circle cx="9" cy="17" r="2.25"></circle><circle cx="15.5" cy="15.5" r="2.25"></circle></svg>',
    favorites:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 19-5.1 2.7 1-5.8L3.8 12l5.8-.8L12 6l2.4 5.2 5.8.8-4.1 3.9 1 5.8Z"></path></svg>',
    events:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="6" width="16" height="13" rx="2"></rect><path d="M8 4v4"></path><path d="M16 4v4"></path><path d="M4 10.5h16"></path></svg>',
    activity:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 13h4l2-5 4 10 2-5h4"></path></svg>',
    health:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20s-6.8-4.5-8.6-8.4C1.9 8.5 4.2 5 7.7 5c1.8 0 3.4.9 4.3 2.3C13 5.9 14.5 5 16.3 5c3.5 0 5.8 3.5 4.3 6.6C18.8 15.5 12 20 12 20Z"></path></svg>',
    topic:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 7.5h11v9h-11z"></path><path d="m6.5 11.5-2 1.5 2 1.5"></path><path d="m17.5 11.5 2 1.5-2 1.5"></path></svg>',
    section:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16"></path><path d="M4 12h11"></path><path d="M4 17h8"></path></svg>',
    memory:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5h8l3 3v12H7z"></path><path d="M15 4.5v3h3"></path><path d="M9.5 12h5"></path><path d="M9.5 15h5"></path></svg>',
  };

  const normalizedIconKey = (iconKey) => {
    const value = String(iconKey || "").trim().toLowerCase();
    if (!value) return "memory";
    if (value.includes("child")) return "child";
    if (value.includes("family")) return "family";
    if (value.includes("friend")) return "friend";
    if (value.includes("pet")) return "pet";
    if (value.includes("artist")) return "artist";
    if (value.includes("favorite")) return "favorites";
    if (value.includes("event")) return "events";
    if (value.includes("health")) return "health";
    if (value.includes("activity")) return "activity";
    if (value.includes("topic")) return "topic";
    if (value.includes("section") || value.includes("facet")) return "section";
    return "memory";
  };

  const iconMarkup = (iconKey, options = {}) => {
    const small = Boolean(options.small);
    const normalized = normalizedIconKey(iconKey);
    const svg = ICONS[normalized] || ICONS.memory;
    return `<span class="memory-node-icon memory-node-icon-${normalized}${small ? " is-small" : ""}">${svg}</span>`;
  };

  const humanize = (value) =>
    String(value || "")
      .replaceAll("_", " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/\b\w/g, (match) => match.toUpperCase());

  const nodeKindLabel = (nodeData) => {
    const kind = String(nodeData?.kind || "memory");
    if (kind === "child") return "Child anchor";
    if (kind === "facet") return "Category branch";
    if (kind === "friend") return "Friend";
    if (kind === "family_member") return "Family member";
    if (kind === "artist") return "Artist";
    if (kind === "pet") return "Pet";
    if (kind === "topic") return "Topic";
    if (kind === "week") return "Week";
    if (kind === "day") return "Day";
    if (kind === "memory") return humanize(nodeData?.memory_type_label || nodeData?.memoryTypeLabel || "Memory");
    return humanize(kind);
  };

  const directNeighborIds = (nodeId) => {
    const ids = new Set();
    structuralEdges().forEach((edge) => {
      if (edge.source === nodeId) ids.add(edge.target);
      if (edge.target === nodeId) ids.add(edge.source);
    });
    return [...ids];
  };

  const gatherDescendantMemories = (startId, limit = 8) => {
    const nodes = nodeIndex();
    const edges = structuralEdges();
    const queue = [startId];
    const visited = new Set([startId]);
    const matches = [];

    while (queue.length && matches.length < limit) {
      const current = queue.shift();
      edges.forEach((edge) => {
        const neighborId =
          edge.source === current ? edge.target : edge.target === current ? edge.source : null;
        if (!neighborId || visited.has(neighborId)) return;
        visited.add(neighborId);
        const neighbor = nodes.get(neighborId);
        if (!neighbor) return;
        if (neighbor.kind === "memory") {
          matches.push(neighbor);
          return;
        }
        queue.push(neighborId);
      });
    }
    return matches;
  };

  const branchNodesFor = (nodeId, limit = 6) => {
    const nodes = nodeIndex();
    return directNeighborIds(nodeId)
      .map((id) => nodes.get(id))
      .filter((node) => node && node.kind !== "memory")
      .slice(0, limit);
  };

  const relatedMemoriesForNode = (nodeId, limit = 6) => {
    const nodes = nodeIndex();
    const direct = directNeighborIds(nodeId)
      .map((id) => nodes.get(id))
      .filter((node) => node && node.kind === "memory");
    const results = [...direct];
    if (results.length < limit) {
      gatherDescendantMemories(nodeId, limit).forEach((node) => {
        if (!results.find((item) => item.id === node.id)) {
          results.push(node);
        }
      });
    }
    return results.slice(0, limit);
  };

  const defaultGraphNode = () => {
    const nodes = Array.isArray(latestGraph.nodes) ? latestGraph.nodes : [];
    if (!nodes.length) return null;
    if (graphView === "routine") {
      return nodes.find((node) => node.kind === "week") || nodes.find((node) => node.kind !== "memory") || nodes[0];
    }
    return (
      nodes.find((node) => node.kind === "child") ||
      nodes.find((node) => node.kind !== "memory") ||
      nodes[0]
    );
  };

  const renderSearchSuggestions = () => {
    if (!searchSuggestions) return;
    const seen = new Set();
    const fragment = [];
    (latestGraph.nodes || []).forEach((node) => {
      const label = String(node.label || "").trim();
      if (!label || seen.has(label.toLowerCase())) return;
      seen.add(label.toLowerCase());
      fragment.push(`<option value="${escapeHtml(label)}"></option>`);
    });
    searchSuggestions.innerHTML = fragment.join("");
  };

  const renderQuickLinks = (nodeData) => {
    if (!nodeQuickLinks) return;
    const quickJumpNodes = [];
    const breadcrumbItems = Array.isArray(nodeData?.breadcrumb) ? nodeData.breadcrumb : [];
    breadcrumbItems.forEach((item) => {
      if (item?.id && item.id !== nodeData.id) {
        const targetNode = nodeIndex().get(item.id);
        if (targetNode) {
          quickJumpNodes.push(targetNode);
        }
      }
    });
    branchNodesFor(nodeData.id, 8).forEach((node) => quickJumpNodes.push(node));
    const deduped = [];
    const seen = new Set();
    quickJumpNodes.forEach((node) => {
      if (!node || seen.has(node.id) || node.id === nodeData.id) return;
      seen.add(node.id);
      deduped.push(node);
    });
    if (!deduped.length) {
      nodeQuickLinks.innerHTML = '<p class="subtle">No nearby branch jumps yet.</p>';
      return;
    }
    nodeQuickLinks.innerHTML = deduped
      .map(
        (node) => `
          <button type="button" class="memory-node-quick-link" data-node-jump="${node.id}">
            ${iconMarkup(node.icon_key || node.iconKey, { small: true })}
            <span>${escapeHtml(node.label)}</span>
          </button>
        `,
      )
      .join("");
    nodeQuickLinks.querySelectorAll("[data-node-jump]").forEach((button) => {
      button.addEventListener("click", () => {
        const nextId = button.dataset.nodeJump || "";
        const nextNode = nodeIndex().get(nextId);
        if (!nextNode) return;
        if (nextNode.kind === "memory") {
          void loadMemory(nextId, { center: true });
          return;
        }
        showNodeInspector(nextNode, { center: true });
      });
    });
  };

  const renderNodeRelatedMemories = (nodeData) => {
    if (!nodeRelatedList) return;
    const memories = relatedMemoriesForNode(nodeData.id, 6);
    if (!memories.length) {
      nodeRelatedList.innerHTML = '<p class="subtle">No memory notes are attached here yet.</p>';
      return;
    }
    nodeRelatedList.innerHTML = memories
      .map(
        (item) => `
          <button type="button" class="memory-linked-item" data-linked-memory-id="${item.id}">
            <div>
              <strong>${escapeHtml(item.label)}</strong>
              <p>${escapeHtml(item.memory_type_label || item.memoryTypeLabel || "Memory")}</p>
              <small>${escapeHtml(item.summary || "")}</small>
            </div>
            <span class="pill">${escapeHtml(item.branch_label || item.branchLabel || "Memory")}</span>
          </button>
        `,
      )
      .join("");
    nodeRelatedList.querySelectorAll("[data-linked-memory-id]").forEach((button) => {
      button.addEventListener("click", () => {
        void loadMemory(button.dataset.linkedMemoryId || "", { center: true });
      });
    });
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
        void loadMemory(button.dataset.linkedMemoryId || "", { center: true });
      });
    });
  };

  const renderRecentMemories = (items) => {
    if (!recentList || !recentEmpty) return;
    const rows = Array.isArray(items) ? items : [];
    if (!rows.length) {
      recentList.hidden = true;
      recentList.innerHTML = "";
      recentEmpty.hidden = false;
      return;
    }
    recentEmpty.hidden = true;
    recentList.hidden = false;
    recentList.innerHTML = rows
      .map(
        (item) => `
          <button type="button" class="memory-library-item" data-memory-id="${item.id}">
            <div class="memory-library-item-head">
              <div>
                <strong>${escapeHtml(item.title)}</strong>
                <p class="subtle">${escapeHtml(item.memory_type_label)} · ${escapeHtml(item.updated_label)}</p>
              </div>
              <div class="memory-library-badges">
                ${item.pinned ? '<span class="pill">Pinned</span>' : ""}
                ${item.archived ? '<span class="pill memory-pill-archived">Archived</span>' : ""}
              </div>
            </div>
            <p>${escapeHtml(item.summary || "")}</p>
          </button>
        `,
      )
      .join("");
    bindMemoryButtons(recentList);
  };

  const updateRecentPagination = () => {
    if (recentPageLabel) {
      recentPageLabel.textContent = `Page ${recentPage} of ${recentPageTotal}`;
    }
    if (recentPrevButton) {
      recentPrevButton.disabled = recentPage <= 1;
    }
    if (recentNextButton) {
      recentNextButton.disabled = recentPage >= recentPageTotal;
    }
  };

  const refreshRecentMemories = async (page = recentPage) => {
    if (!recentList && !recentEmpty) return;
    const response = await requestJson(recentRequestUrl(page));
    recentPage = Number(response.page || 1) || 1;
    recentPageTotal = Number(response.page_total || 1) || 1;
    renderRecentMemories(response.items || []);
    updateRecentPagination();
  };

  const renderFitSection = (memory) => {
    if (fitSummary) {
      const bits = [];
      if (memory.primary_entity) {
        const relationLabel = memory.primary_entity.relation_to_child
          ? ` as ${humanize(memory.primary_entity.relation_to_child)}`
          : "";
        bits.push(`${memory.title} is anchored under ${memory.primary_entity.display_name}${relationLabel}.`);
      }
      if (Array.isArray(memory.semantic_path) && memory.semantic_path.length) {
        bits.push(`Path: ${memory.semantic_path.join(" / ")}.`);
      }
      if (memory.semantic_group) {
        bits.push(`Group: ${humanize(memory.semantic_group)}.`);
      }
      if (!bits.length) {
        bits.push("This memory is stored in Resona's long-term memory map and can connect to nearby people, branches, and related notes.");
      }
      fitSummary.textContent = bits.join(" ");
    }
    if (!fitChips) return;
    const chips = [];
    if (memory.primary_entity) {
      chips.push({
        label: memory.primary_entity.display_name,
        tone: "primary",
        iconKey: memory.primary_entity.entity_kind || memory.primary_entity.semantic_group || "memory",
      });
    }
    (Array.isArray(memory.attached_entities) ? memory.attached_entities : []).forEach((entity) => {
      if (memory.primary_entity && entity.id === memory.primary_entity.id) return;
      chips.push({
        label: entity.display_name,
        tone: "secondary",
        iconKey: entity.entity_kind || entity.semantic_group || "memory",
      });
    });
    (Array.isArray(memory.tags) ? memory.tags : []).slice(0, 4).forEach((tag) => {
      chips.push({ label: `#${tag}`, tone: "secondary", iconKey: "topic" });
    });
    fitChips.innerHTML = chips
      .slice(0, 8)
      .map(
        (chip) => `
          <span class="memory-entity-chip ${chip.tone}">
            ${iconMarkup(chip.iconKey, { small: true })}
            <span>${escapeHtml(chip.label)}</span>
          </span>
        `,
      )
      .join("");
  };

  const fillInspector = (memory) => {
    selectedNodeId = memory.id;
    selectedMemoryId = memory.id;
    syncBrowserQuery();
    if (inspectorEmpty) inspectorEmpty.hidden = true;
    if (inspectorNode) inspectorNode.hidden = true;
    if (inspectorBody) inspectorBody.hidden = false;
    renderBreadcrumbs(memoryBreadcrumbs, memory.breadcrumb || []);
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
    renderFitSection(memory);
    renderLinkedMemories(Array.isArray(memory.linked_memories) ? memory.linked_memories : []);
    clearDeletePreview();
    setStatus("", "info");
    updateGraphFocus();
  };

  const showNodeInspector = (nodeData, options = {}) => {
    selectedNodeId = nodeData.id;
    selectedMemoryId = null;
    syncBrowserQuery();
    if (inspectorEmpty) inspectorEmpty.hidden = true;
    if (inspectorBody) inspectorBody.hidden = true;
    if (inspectorNode) inspectorNode.hidden = false;
    renderBreadcrumbs(nodeBreadcrumbs, nodeData.breadcrumb || []);
    if (nodeIconWrap) {
      nodeIconWrap.innerHTML = iconMarkup(nodeData.icon_key || nodeData.iconKey || nodeData.kind);
    }
    if (nodeTitle) {
      nodeTitle.textContent = nodeData.label || "Selected node";
    }
    if (nodeMeta) {
      const bits = [nodeKindLabel(nodeData)];
      const count = Number(nodeData.item_count || nodeData.itemCount || 0);
      if (count > 0) {
        bits.push(`${count} memor${count === 1 ? "y" : "ies"}`);
      }
      nodeMeta.textContent = bits.join(" · ");
    }
    if (nodeSummary) {
      nodeSummary.textContent =
        nodeData.summary ||
        "This part of the memory web helps organize related people, themes, and long-term notes.";
    }
    const relatedMemories = relatedMemoriesForNode(nodeData.id, 6);
    const nearbyBranches = branchNodesFor(nodeData.id, 8);
    if (nodeMemoryCount) nodeMemoryCount.textContent = String(relatedMemories.length);
    if (nodeBranchCount) nodeBranchCount.textContent = String(nearbyBranches.length);
    renderQuickLinks(nodeData);
    renderNodeRelatedMemories(nodeData);
    if (openBranchLink) {
      if (nodeData.kind === "memory") {
        openBranchLink.hidden = true;
        openBranchLink.removeAttribute("href");
      } else {
        openBranchLink.hidden = false;
        openBranchLink.href = memoryLibraryHrefForNode(nodeData.id);
      }
    }
    clearDeletePreview();
    setStatus("", "info");
    updateGraphFocus({ center: Boolean(options.center) });
  };

  const loadMemory = async (memoryId, options = {}) => {
    if (!memoryId) return;
    selectedNodeId = memoryId;
    selectedMemoryId = memoryId;
    syncBrowserQuery();
    updateGraphFocus({ center: Boolean(options.center) });
    setStatus("Loading memory...", "info");
    try {
      const response = await requestJson(`${payload.detail_base_url}/${memoryId}`);
      fillInspector(response.memory);
      void refreshRecentMemories(recentPage);
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
      if (deletedIds.has(selectedNodeId)) {
        selectedNodeId = null;
      }
      document.querySelectorAll("[data-memory-id]").forEach((button) => {
        if (deletedIds.has(button.dataset.memoryId || "")) {
          button.remove();
        }
      });
      clearDeletePreview();
      showEmptyInspector();
      syncBrowserQuery();
      setStatus(`Removed ${response.preview.deleted_count} memory${response.preview.deleted_count === 1 ? "" : "ies"}.`, "success");
      await refreshGraph();
      await refreshRecentMemories(recentPage);
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
      await refreshRecentMemories(recentPage);
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
    if (meta) {
      meta.textContent = `${memory.memory_type_label} · ${memory.updated_at ? `Updated ${formatDateTime(memory.updated_at)}` : "Recently updated"}`;
    }
  };

  const clearSelection = () => {
    selectedNodeId = null;
    selectedMemoryId = null;
    hoveredNodeId = null;
    syncBrowserQuery();
    showEmptyInspector();
    clearDeletePreview();
    updateGraphFocus();
    setStatus("", "info");
  };

  const centerOnNode = (nodeId) => {
    if (!graph || !nodeId) return;
    const node = graph.$id(nodeId);
    if (!node || node.empty()) return;
    const focus = node.closedNeighborhood();
    graph.animate(
      {
        fit: {
          eles: focus,
          padding: 76,
        },
        duration: 260,
      },
      {
        queue: false,
      },
    );
  };

  const applyGraphFocus = (nodeId) => {
    if (!graph) return;
    graph.nodes().removeClass("is-selected is-focus is-neighborhood is-dimmed");
    graph.edges().removeClass("is-focus is-neighborhood is-dimmed");
    if (!nodeId) return;
    const node = graph.$id(nodeId);
    if (!node || node.empty()) return;
    const neighborhood = node.closedNeighborhood();
    graph.nodes().addClass("is-dimmed");
    graph.edges().addClass("is-dimmed");
    neighborhood.nodes().removeClass("is-dimmed").addClass("is-neighborhood");
    neighborhood.edges().removeClass("is-dimmed").addClass("is-neighborhood");
    node.removeClass("is-dimmed").addClass("is-selected is-focus");
    node.connectedEdges().removeClass("is-dimmed").addClass("is-focus");
  };

  const updateGraphFocus = (options = {}) => {
    const nodeId = activeFocusNodeId();
    applyGraphFocus(nodeId);
    if (options.center && nodeId) {
      centerOnNode(nodeId);
    }
  };

  const selectGraphNode = (nodeId, options = {}) => {
    const nextNode = nodeIndex().get(nodeId);
    if (!nextNode) return;
    if (nextNode.kind === "memory") {
      void loadMemory(nodeId, { center: options.center !== false });
      return;
    }
    showNodeInspector(nextNode, { center: options.center !== false });
  };

  const runSearchJump = () => {
    const rawQuery = String(searchInput?.value || "").trim();
    if (!rawQuery) {
      clearSelection();
      return;
    }
    const normalized = rawQuery.toLowerCase();
    const nodes = Array.isArray(latestGraph.nodes) ? latestGraph.nodes : [];
    const exact = nodes.find((node) => String(node.label || "").toLowerCase() === normalized);
    const startsWith = nodes.find((node) => String(node.label || "").toLowerCase().startsWith(normalized));
    const includes = nodes.find((node) => {
      const haystack = [
        node.label,
        node.summary,
        node.semantic_label,
        ...(Array.isArray(node.semantic_path) ? node.semantic_path : []),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(normalized);
    });
    const match = exact || startsWith || includes;
    if (!match) {
      setStatus("Nothing matched that search.", "warning");
      return;
    }
    selectGraphNode(match.id, { center: true });
  };

  const refreshGraph = async () => {
    if (!graphElement) return;
    try {
      const data = await requestJson(graphRequestUrl());
      latestGraph = data;
      renderSearchSuggestions();
      renderGraph(data);
    } catch (error) {
      if (graph) {
        graph.destroy();
        graph = null;
      }
      if (graphElement) {
        graphElement.innerHTML = "";
      }
      if (graphEmpty) {
        graphEmpty.hidden = false;
        const paragraph = graphEmpty.querySelector("p");
        if (paragraph) {
          paragraph.textContent = error.message || "The memory graph is unavailable right now.";
        }
      }
    }
  };

  const renderGraph = (data) => {
    if (!graphElement || typeof window.cytoscape === "undefined") return;
    const nodes = Array.isArray(data.nodes) ? data.nodes : [];
    const structural = Array.isArray(data.structural_edges) ? data.structural_edges : [];
    const similarity = Array.isArray(data.similarity_edges) ? data.similarity_edges : [];
    if (!nodes.length) {
      graphElement.innerHTML = "";
      if (graph) {
        graph.destroy();
        graph = null;
      }
      if (graphEmpty) graphEmpty.hidden = false;
      showEmptyInspector();
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
          memoryTypeLabel: node.memory_type_label,
          kind: node.kind || "memory",
          itemCount: node.item_count || 0,
          breadcrumb: node.breadcrumb || [],
          iconKey: node.icon_key || "",
          branchLabel: node.branch_label || "",
        },
        classes: [
          node.pinned ? "is-pinned" : "",
          node.archived ? "is-archived" : "",
          `is-${node.kind || "memory"}`,
        ]
          .join(" ")
          .trim(),
      })),
      ...structural.map((edge) => ({
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: edge.label || "",
        },
        classes: [
          "is-structural",
          edge.relationship_type || "",
          String(edge.relationship_type || "").startsWith("time_") ? "is-time" : "",
          ["person_cluster", "topic_member", "person_memory", "facet_group", "facet_entity", "facet_memory"].includes(
            String(edge.relationship_type || ""),
          )
            ? "is-anchor"
            : "",
        ]
          .join(" ")
          .trim(),
      })),
      ...similarity.map((edge) => ({
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
            "font-weight": 600,
            "text-wrap": "wrap",
            "text-max-width": 96,
            "text-valign": "center",
            "text-halign": "center",
            width: 58,
            height: 58,
            padding: 8,
            "transition-property": "opacity, background-color, border-color, border-width",
            "transition-duration": "160ms",
          },
        },
        {
          selector: "node.is-week",
          style: {
            shape: "round-rectangle",
            width: 124,
            height: 48,
            "background-color": "#e7efe7",
            "border-color": "#516b62",
            "font-size": 12,
            "text-max-width": 108,
          },
        },
        {
          selector: "node.is-day",
          style: {
            shape: "round-rectangle",
            width: 106,
            height: 40,
            "background-color": "#f4efe5",
            "border-color": "#8d7a5f",
            "font-size": 11,
            "text-max-width": 94,
          },
        },
        {
          selector: "node.is-child",
          style: {
            shape: "ellipse",
            width: 92,
            height: 92,
            "background-color": "#e8f1ee",
            "border-color": "#1e5f53",
            "font-size": 13,
            "font-weight": 700,
            "text-max-width": 78,
          },
        },
        {
          selector: "node.is-facet",
          style: {
            shape: "round-rectangle",
            width: 112,
            height: 52,
            "background-color": "#f0eee8",
            "border-color": "#76684f",
            "font-size": 11,
            "text-max-width": 98,
          },
        },
        {
          selector: "node.is-friend, node.is-family_member, node.is-pet, node.is-artist, node.is-topic",
          style: {
            shape: "ellipse",
            width: 84,
            height: 84,
            "background-color": "#eef4f1",
            "border-color": "#345f56",
            "font-size": 12,
            "text-max-width": 74,
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
            opacity: 0.5,
            "background-color": "#f1ebe1",
          },
        },
        {
          selector: "node.is-selected, node.is-focus",
          style: {
            "background-color": "#eaf2ef",
            "border-color": "#1e5f53",
            "border-width": 4,
          },
        },
        {
          selector: "node.is-neighborhood",
          style: {
            opacity: 1,
          },
        },
        {
          selector: "node.is-dimmed",
          style: {
            opacity: 0.18,
            "text-opacity": 0.35,
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
            "transition-property": "opacity, width, line-color",
            "transition-duration": "160ms",
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
          selector: "edge.is-anchor",
          style: {
            width: 1.8,
            "line-color": "#a39b8d",
            opacity: 0.72,
          },
        },
        {
          selector: "edge.is-time",
          style: {
            width: 2,
            "line-color": "#c4bbad",
            opacity: 0.8,
          },
        },
        {
          selector: "edge.is-similarity",
          style: {
            width: 1.5,
            "line-style": "dashed",
            "line-color": "#9ea7a4",
            opacity: 0.7,
          },
        },
        {
          selector: "edge.is-neighborhood",
          style: {
            opacity: 0.95,
          },
        },
        {
          selector: "edge.is-focus",
          style: {
            width: 3.2,
            opacity: 1,
            "line-color": "#1e5f53",
          },
        },
        {
          selector: "edge.is-dimmed",
          style: {
            opacity: 0.08,
          },
        },
      ],
      layout:
        graphView === "routine"
          ? {
              name: "breadthfirst",
              animate: true,
              animationDuration: 360,
              fit: true,
              padding: 28,
              directed: true,
              spacingFactor: 1.15,
              avoidOverlap: true,
              roots: "[kind = 'week']",
            }
          : {
              name: "cose",
              animate: true,
              animationDuration: 380,
              fit: true,
              padding: 32,
              nodeRepulsion: 18000,
              idealEdgeLength: 116,
              edgeElasticity: 110,
              gravity: 0.18,
              componentSpacing: 70,
              nestingFactor: 0.95,
              randomize: false,
              avoidOverlap: true,
            },
    });

    graph.on("mouseover", "node", (event) => {
      hoveredNodeId = event.target.id();
      updateGraphFocus();
    });

    graph.on("mouseout", "node", () => {
      hoveredNodeId = null;
      updateGraphFocus();
    });

    graph.on("tap", "node", (event) => {
      hoveredNodeId = null;
      const nodeId = event.target.id();
      selectGraphNode(nodeId, { center: true });
    });

    graph.on("tap", (event) => {
      if (event.target === graph) {
        clearSelection();
      }
    });

    const existingSelectedNode = selectedNodeId ? nodeIndex().get(selectedNodeId) : null;
    if (existingSelectedNode) {
      if (existingSelectedNode.kind === "memory") {
        void loadMemory(existingSelectedNode.id, { center: false });
      } else {
        showNodeInspector(existingSelectedNode, { center: false });
      }
    } else if (selectedNodeId) {
      clearSelection();
    } else {
      const initialNode = defaultGraphNode();
      if (initialNode && initialNode.kind !== "memory") {
        showNodeInspector(initialNode, { center: false });
      } else if (initialNode) {
        void loadMemory(initialNode.id, { center: false });
      } else {
        updateGraphFocus();
      }
    }
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
      syncBrowserQuery();
      void refreshGraph();
      void refreshRecentMemories(1);
    });
  }
  if (showSimilarityToggle) {
    showSimilarityToggle.addEventListener("change", () => {
      syncBrowserQuery();
      void refreshGraph();
    });
  }
  if (searchSubmit) {
    searchSubmit.addEventListener("click", () => {
      runSearchJump();
    });
  }
  if (searchInput) {
    searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        runSearchJump();
      }
    });
  }
  if (fitButton) {
    fitButton.addEventListener("click", () => {
      if (graph) {
        graph.fit(undefined, 56);
      }
    });
  }
  if (centerChildButton) {
    centerChildButton.addEventListener("click", () => {
      const childNode = (latestGraph.nodes || []).find((node) => node.kind === "child") || latestGraph.nodes?.[0];
      if (!childNode) return;
      showNodeInspector(childNode, { center: true });
    });
  }
  if (clearFocusButton) {
    clearFocusButton.addEventListener("click", () => {
      clearSelection();
    });
  }
  if (legendToggle && legendPanel) {
    legendToggle.addEventListener("click", () => {
      legendPanel.hidden = !legendPanel.hidden;
    });
  }

  bindMemoryButtons();

  if (recentPrevButton) {
    recentPrevButton.addEventListener("click", () => {
      if (recentPage <= 1) return;
      void refreshRecentMemories(recentPage - 1);
    });
  }
  if (recentNextButton) {
    recentNextButton.addEventListener("click", () => {
      if (recentPage >= recentPageTotal) return;
      void refreshRecentMemories(recentPage + 1);
    });
  }
  updateRecentPagination();

  if (graphElement) {
    void refreshGraph();
  } else if (selectedNodeId) {
    void loadMemory(selectedNodeId, { center: false });
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
