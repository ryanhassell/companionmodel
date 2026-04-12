import {
  RequestFailureCode,
  createPortalBanner,
  currentPathWithQuery,
  fetchJson,
  publishSessionEvent,
  retryWithBackoff,
  watchConnectivity,
} from "/static/portal-resilience.js";

(function () {
  const root = document.querySelector("[data-portal-initialize]");
  if (!root) {
    return;
  }

  document.documentElement.classList.add("js");

  const payloadNode = document.getElementById("portal-initialize-data");
  if (!payloadNode) {
    return;
  }

  let payload;
  try {
    payload = JSON.parse(payloadNode.textContent || "{}");
  } catch {
    return;
  }

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const banner = createPortalBanner();
  const form = document.getElementById("portal-initialize-form");
  const saveIndicator = document.getElementById("initialization-save-indicator");
  const currentMeta = document.getElementById("initialization-step-meta");
  const currentTitle = document.getElementById("initialization-step-title");
  const currentDescription = document.getElementById("initialization-step-description");
  const backButton = document.getElementById("initialization-back-button");
  const nextButton = document.getElementById("initialization-next-button");
  const billingButton = document.getElementById("initialization-billing-button");
  const billingSelectedPlan = document.getElementById("billing-selected-plan");
  const billingSubscriptionStatus = document.getElementById("billing-subscription-status");
  const panels = Array.from(root.querySelectorAll("[data-step-panel]"));
  const indicators = Array.from(root.querySelectorAll("[data-step-indicator]"));
  const summaryNodes = Array.from(root.querySelectorAll("[data-summary-field]"));
  const fieldErrors = Array.from(root.querySelectorAll("[data-error-for]"));
  const previewBoxes = Array.from(root.querySelectorAll("[data-preview-box]"));
  const aiPreviewBox = document.getElementById("initialization-ai-preview");
  const aiPreviewMessage = document.getElementById("initialization-ai-preview-message");
  const aiPreviewCaption = document.getElementById("initialization-ai-preview-caption");
  const resonaPreviewButton = document.getElementById("initialization-resona-preview-button");
  const resonaPreviewMessage = document.getElementById("initialization-resona-preview-message");
  const resonaPreviewCaption = document.getElementById("initialization-resona-preview-caption");
  const resonaPreviewAudio = document.getElementById("initialization-resona-preview-audio");
  const resonaPresetPanel = root.querySelector("[data-resona-preset-panel]");

  const steps = Array.isArray(payload.steps) ? payload.steps : [];
  const visibleSteps = steps.filter((step) => step.key !== "complete");
  const stepOrder = Array.isArray(payload.step_order) ? payload.step_order : [];
  const stepMap = new Map(steps.map((step) => [step.key, step]));
  const planMap = new Map(
    (Array.isArray(payload.plan_options) ? payload.plan_options : []).map((plan) => [plan.key, plan])
  );
  const voiceProfiles = Array.isArray(payload.voice_profiles) ? payload.voice_profiles : [];
  const voiceMap = new Map(voiceProfiles.map((voice) => [voice.key, voice]));
  const resonaPresets = Array.isArray(payload.resona_presets) ? payload.resona_presets : [];
  const resonaPresetMap = new Map(resonaPresets.map((preset) => [preset.key, preset]));
  const previewTimers = new WeakMap();
  const aiPreviewCache = new Map();
  const resonaPreviewCache = new Map();
  const DRAFT_TTL_MS = 1000 * 60 * 90;

  let activeStep = payload.current_step || root.getAttribute("data-current-step") || "welcome";
  let completedSteps = new Set(Array.isArray(payload.completed_steps) ? payload.completed_steps : []);
  let snapshot = payload.snapshot || {};
  let summary = payload.summary || {};
  let billingStatus = payload.billing_status || "incomplete";
  let inFlight = false;
  let billingInFlight = false;
  let autosaveTimer = null;
  let pendingAutosaveStep = "";
  let previewPendingOnReconnect = false;
  let pendingPreviewPayload = null;
  let lastSavedAt = payload.server_saved_at ? new Date(payload.server_saved_at) : new Date();
  let previewRequestTimer = null;
  let pendingPreviewKey = "";

  const stepFields = {
    household: ["mode", "relationship", "household_name", "timezone"],
    child: ["profile_name", "child_phone_number", "birth_year", "notes"],
    resona: [
      "resona_mode",
      "resona_preset_key",
      "resona_display_name",
      "resona_voice_profile_key",
      "resona_vibe",
      "resona_support_style",
      "resona_avoid",
      "resona_anchors",
      "resona_proactive_style",
    ],
    preferences: [
      "preferred_pacing",
      "preferred_pacing_custom",
      "response_style",
      "response_style_custom",
      "voice_enabled",
      "proactive_check_ins",
      "parent_visibility_mode",
      "alert_threshold",
      "quiet_hours_start",
      "quiet_hours_end",
      "daily_cadence",
      "communication_notes",
    ],
    plan: ["selected_plan_key"],
  };

  const draftStorageKey = () => `resona:portal:init-draft:${payload.account_scope || "account"}`;

  const allDraftFields = Array.from(new Set(Object.values(stepFields).flat()));

  const reportDraftEvent = async (eventType) => {
    if (!payload.draft_event_url || !payload.csrf_token) {
      return;
    }
    try {
      await fetchJson(payload.draft_event_url, {
        method: "POST",
        credentials: "same-origin",
        timeoutMs: 4000,
        resumeUrl: payload.resume_url || currentPathWithQuery(),
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          csrf_token: payload.csrf_token,
          event_type: eventType,
        }),
      });
    } catch {
      // Draft event logging should never interrupt setup recovery.
    }
  };

  const readStoredDraft = () => {
    try {
      const raw = window.sessionStorage.getItem(draftStorageKey());
      if (!raw) {
        return null;
      }
      const draft = JSON.parse(raw);
      const savedAt = Number(draft?.saved_at || 0);
      if (!savedAt || Date.now() - savedAt > DRAFT_TTL_MS) {
        window.sessionStorage.removeItem(draftStorageKey());
        return null;
      }
      if (draft?.account_scope && draft.account_scope !== payload.account_scope) {
        return null;
      }
      return draft;
    } catch {
      return null;
    }
  };

  const clearStoredDraft = () => {
    try {
      window.sessionStorage.removeItem(draftStorageKey());
    } catch {
      // Ignore private browsing storage failures.
    }
  };

  const serverSavedAtMs = () => {
    const value = Date.parse(payload.server_saved_at || "");
    return Number.isFinite(value) ? value : 0;
  };

  const collectDraftSnapshot = () => {
    const next = { ...snapshot };
    allDraftFields.forEach((name) => {
      next[name] = readFieldValue(name);
    });
    return next;
  };

  const persistDraft = () => {
    try {
      window.sessionStorage.setItem(
        draftStorageKey(),
        JSON.stringify({
          step: activeStep,
          snapshot: collectDraftSnapshot(),
          saved_at: Date.now(),
          resume_url: payload.resume_url || currentPathWithQuery(),
          account_scope: payload.account_scope || "",
        })
      );
    } catch {
      // Ignore private browsing storage failures.
    }
  };

  const discardRecoveredDraft = () => {
    clearStoredDraft();
    banner.hide();
    reportDraftEvent("discarded");
  };

  const restoreStoredDraftIfNewer = () => {
    const draft = readStoredDraft();
    if (!draft) {
      return;
    }
    if (serverSavedAtMs() && Number(draft.saved_at || 0) <= serverSavedAtMs()) {
      clearStoredDraft();
      return;
    }
    snapshot = {
      ...snapshot,
      ...(typeof draft.snapshot === "object" && draft.snapshot ? draft.snapshot : {}),
    };
    if (draft.step && stepMap.has(draft.step) && draft.step !== "complete") {
      activeStep = draft.step;
    }
    reportDraftEvent("restored");
    banner.show({
      tone: "success",
      title: "Recovered your in-progress setup",
      message: "Your changes are safe in this tab, and we restored the latest draft we found here.",
      actions: [
        { label: "Discard recovered draft", onClick: () => discardRecoveredDraft(), variant: "ghost" },
      ],
    });
  };

  const redirectToLogin = (loginUrl, reasonMessage) => {
    persistDraft();
    publishSessionEvent("session_expired", {
      path: payload.resume_url || currentPathWithQuery(),
    });
    banner.show({
      tone: "warning",
      title: "Session expired",
      message: reasonMessage,
    });
    window.setTimeout(() => {
      window.location.assign(loginUrl || `/app/login?reason=invalid_session&resume=${encodeURIComponent(payload.resume_url || currentPathWithQuery())}`);
    }, 500);
  };

  const humanize = (value) =>
    String(value || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (match) => match.toUpperCase());

  const planLabel = (planKey) => {
    const key = String(planKey || "").trim().toLowerCase();
    if (!key) {
      return "Not selected yet";
    }
    return planMap.get(key)?.label || humanize(key);
  };

  const visibilityLabel = (value) => {
    if (value === "full_transcript") {
      return "Full transcript + events";
    }
    if (value === "summary_with_alerts") {
      return "Summary + alerts emphasis";
    }
    return humanize(value || "Not set yet");
  };

  const summaryValue = (field, value) => {
    if (Array.isArray(value)) {
      return value.length > 0 ? value.map((item) => humanize(item)).join(", ") : "Not set yet";
    }
    if (field === "selected_plan_key") {
      return planLabel(value);
    }
    if (field === "subscription_status") {
      return humanize(value || "incomplete");
    }
    if (field === "relationship_label") {
      return humanize(value || "Not set yet");
    }
    if (field === "resona_voice_label") {
      if (value) {
        return value;
      }
      const selectedVoice = voiceMap.get(String(snapshot.resona_voice_profile_key || "").trim());
      return selectedVoice?.label || "Not set yet";
    }
    if (field === "parent_visibility_mode") {
      return visibilityLabel(value);
    }
    if (field === "preferred_pacing") {
      return value || "Not set yet";
    }
    return value || "Not set yet";
  };

  const formatSavedAt = (date) => {
    const formatted = new Intl.DateTimeFormat([], {
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    })
      .format(date)
      .replace(/\s?(AM|PM)$/i, (match) => match.trim().toLowerCase());
    return `Saved ${formatted}`;
  };

  const setSaveState = (state, label) => {
    if (!saveIndicator) {
      return;
    }
    saveIndicator.dataset.state = state;
    saveIndicator.textContent = label;
  };

  const readFieldValue = (name) => {
    const nodes = Array.from(form.querySelectorAll(`[name="${name}"]`));
    if (nodes.length === 0) {
      return "";
    }
    const first = nodes[0];
    if (first.type === "radio") {
      return form.querySelector(`[name="${name}"]:checked`)?.value || "";
    }
    if (first.type === "checkbox") {
      if (nodes.length > 1) {
        return nodes.filter((node) => node.checked).map((node) => node.value);
      }
      return first.checked;
    }
    return first.value;
  };

  const animatePreviewText = (node, text) => {
    if (!node) {
      return;
    }
    const nextText = String(text || "");
    if (node.dataset.lastText === nextText) {
      return;
    }
    node.dataset.lastText = nextText;
    const existingTimer = previewTimers.get(node);
    if (existingTimer) {
      window.clearTimeout(existingTimer);
      previewTimers.delete(node);
    }
    if (reduceMotion) {
      node.textContent = nextText;
      return;
    }
    node.textContent = "";
    let index = 0;
    const typeNext = () => {
      index += 1;
      node.textContent = nextText.slice(0, index);
      if (index < nextText.length) {
        const timer = window.setTimeout(typeNext, 8);
        previewTimers.set(node, timer);
      } else {
        previewTimers.delete(node);
      }
    };
    typeNext();
  };

  const variantIndexFor = (seed, length) => {
    const text = String(seed || "");
    let hash = 7;
    for (let i = 0; i < text.length; i += 1) {
      hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    }
    return length > 0 ? hash % length : 0;
  };

  const pickVariant = (variants, seed) => {
    if (!Array.isArray(variants) || variants.length === 0) {
      return "";
    }
    return variants[variantIndexFor(seed, variants.length)] || variants[0];
  };

  const buildPreviewContent = (key) => {
    const childNameRaw = String(readFieldValue("profile_name") || snapshot.profile_name || "").trim();
    const childName = childNameRaw || "there";
    const childLabel = childNameRaw || "your child";
    const preferredPacingValues = Array.isArray(readFieldValue("preferred_pacing"))
      ? readFieldValue("preferred_pacing")
      : Array.isArray(snapshot.preferred_pacing)
        ? snapshot.preferred_pacing
        : [];
    const responseStyleValues = Array.isArray(readFieldValue("response_style"))
      ? readFieldValue("response_style")
      : Array.isArray(snapshot.response_style)
        ? snapshot.response_style
        : [];
    const preferredPacing = String(preferredPacingValues[0] || "");
    const responseStyle = String(responseStyleValues[0] || "");
    const proactiveCheckIns = Boolean(readFieldValue("proactive_check_ins"));
    const parentVisibilityMode = String(readFieldValue("parent_visibility_mode") || snapshot.parent_visibility_mode || "");
    const alertThreshold = String(readFieldValue("alert_threshold") || snapshot.alert_threshold || "");
    const quietHoursStart = String(readFieldValue("quiet_hours_start") || snapshot.quiet_hours_start || "");
    const quietHoursEnd = String(readFieldValue("quiet_hours_end") || snapshot.quiet_hours_end || "");
    const dailyCadence = String(readFieldValue("daily_cadence") || snapshot.daily_cadence || "");
    const selectedPlanKey = String(readFieldValue("selected_plan_key") || snapshot.selected_plan_key || "");

    if (key === "preferred_pacing") {
      if (!preferredPacing) {
        return { visible: false, message: "", caption: "" };
      }
      const pacingExamples = {
        gentle: [
          `Hey ${childName}, take your time. What happened?`,
          `${childName}, no rush. You can tell me a little bit at a time.`,
          `Hi ${childName}. We can go slowly. What do you want to say first?`,
          `${childName}, it's okay to start small. What happened today?`,
          `Hey ${childName}, you don't have to say it all at once.`,
          `${childName}, we can keep this nice and easy. What happened?`,
        ],
        balanced: [
          `Tell me what happened, ${childName}. We can go one step at a time.`,
          `Hey ${childName}, what happened today? We'll work through it together.`,
          `${childName}, start wherever you want. I'll help you with the rest.`,
          `Okay ${childName}, tell me the main part first.`,
          `${childName}, what happened? We can keep it simple.`,
          `Hey ${childName}, what do you want to talk about first?`,
        ],
        direct: [
          `${childName}, tell me the main thing first.`,
          `Hey ${childName}, what's the biggest part of this?`,
          `${childName}, what happened first, and what's the hard part now?`,
          `Let's keep it simple, ${childName}. What happened?`,
          `${childName}, what's the problem right now?`,
          `Quick version first, ${childName}. Then we can keep going.`,
        ],
        reflective: [
          `${childName}, what part of today is still on your mind?`,
          `When you think about today, ${childName}, what stands out the most?`,
          `${childName}, what part is still bothering you a little?`,
          `What feeling is sticking around right now, ${childName}?`,
          `${childName}, what moment are you still thinking about?`,
          `When you look back on today, ${childName}, what feels important?`,
        ],
        playful: [
          `Okay ${childName}, tell me the biggest part of your day.`,
          `${childName}, was today more good, bad, or just weird?`,
          `Hey ${childName}, what was the most interesting part of today?`,
          `${childName}, tell me the part you want to talk about first.`,
          `Okay ${childName}, what was the big moment today?`,
          `${childName}, what part of today feels most worth talking about?`,
        ],
        steady: [
          `${childName}, let's go in order. What happened first?`,
          `Start at the beginning, ${childName}, and we'll go step by step.`,
          `${childName}, tell me what happened first, then what happened next.`,
          `One step at a time, ${childName}. What came first?`,
          `${childName}, we can keep this simple. Start at the beginning.`,
          `Hey ${childName}, walk me through it slowly, one part at a time.`,
        ],
      };
      const fallbackStyle = responseStyle || "warm";
      const styleClosers = {
        warm: [
          "I'm here with you.",
          "Thank you for telling me.",
          "I'm listening.",
          "It's okay. I'm with you.",
          "I'm glad you told me.",
          "That makes sense.",
        ],
        calm: [
          "We can take this slowly.",
          "No rush.",
          "One step at a time is okay.",
          "We can keep this simple.",
          "Let's slow it down a little.",
          "It's okay to go piece by piece.",
        ],
        encouraging: [
          "We'll figure it out together.",
          "You can do this.",
          "We'll take the next step together.",
          "You're doing okay.",
          "We can keep going.",
          "You're not alone in this.",
        ],
        reassuring: [
          "You're okay.",
          "It's okay to talk about this.",
          "You're not in trouble.",
          "It makes sense that you feel that way.",
          "I'm still here.",
          "You didn't do anything wrong by telling me.",
        ],
        upbeat: [
          "We've got this.",
          "Let's work through it together.",
          "We can handle this.",
          "Let's keep going.",
          "Let's figure it out.",
          "Still with you.",
        ],
        straightforward: [
          "Let's keep it simple.",
          "Let's talk about the main part.",
          "We'll figure out the problem together.",
          "Let's stay clear and simple.",
          "Tell me the main thing.",
          "Let's work on what's in front of us.",
        ],
      };
      const pacingMessage = pickVariant(
        pacingExamples[preferredPacing] || pacingExamples.balanced,
        `${childName}|${preferredPacing}|${responseStyle}|pacing`
      );
      const closer = pickVariant(
        styleClosers[fallbackStyle] || styleClosers.warm,
        `${childName}|${preferredPacing}|${responseStyle}|closer`
      );
      return {
        visible: true,
        message: `${pacingMessage} ${closer}`,
        caption: `${humanize(preferredPacing)} pacing shapes how quickly Resona moves ${childLabel} through the conversation.`,
      };
    }

    if (key === "response_style") {
      if (!responseStyle) {
        return { visible: false, message: "", caption: "" };
      }
      const styleResponses = {
        warm: [
          `Hey ${childName}, thank you for telling me. I'm here with you.`,
          `That makes sense, ${childName}. I'm listening.`,
          `I'm glad you told me, ${childName}.`,
          `Hey ${childName}, we can talk about it together.`,
          `I'm right here, ${childName}.`,
          `Thank you for saying that, ${childName}.`,
        ],
        calm: [
          `${childName}, let's slow down and take one part at a time.`,
          `No rush, ${childName}.`,
          `We can keep this simple, ${childName}.`,
          `${childName}, tell me one small part first.`,
          `It's okay to go slowly, ${childName}.`,
          `We don't have to do it all at once, ${childName}.`,
        ],
        encouraging: [
          `${childName}, you can do this. We'll take the next step together.`,
          `We're okay, ${childName}. We'll work through it.`,
          `${childName}, one step is enough right now.`,
          `Let's keep going together, ${childName}.`,
          `You're doing okay, ${childName}.`,
          `${childName}, we'll figure it out one part at a time.`,
        ],
        reassuring: [
          `${childName}, you're okay.`,
          `It's okay to feel like this, ${childName}.`,
          `You're safe to talk about it, ${childName}.`,
          `You're not in trouble, ${childName}.`,
          `It makes sense that you feel that way, ${childName}.`,
          `${childName}, I'm still here with you.`,
        ],
        upbeat: [
          `Okay ${childName}, let's work through it together.`,
          `${childName}, we've got this.`,
          `Hey ${childName}, let's see what helps next.`,
          `${childName}, we can handle this one step at a time.`,
          `Alright ${childName}, let's keep going.`,
          `Still with you, ${childName}.`,
        ],
        straightforward: [
          `${childName}, tell me the main thing.`,
          `Let's keep it simple, ${childName}. What happened?`,
          `${childName}, what's the hard part right now?`,
          `Tell me the clearest version, ${childName}.`,
          `${childName}, what's going on right now?`,
          `Let's talk about the main part first, ${childName}.`,
        ],
      };
      return {
        visible: true,
        message: pickVariant(styleResponses[responseStyle] || styleResponses.warm, `${childName}|${responseStyle}|style`),
        caption: `${humanize(responseStyle)} tone changes how emotionally supportive the reply feels for ${childLabel}.`,
      };
    }

    if (key === "parent_visibility_mode") {
      if (!parentVisibilityMode) {
        return { visible: false, message: "", caption: "" };
      }
      const visibilityCopy =
        parentVisibilityMode === "summary_with_alerts"
          ? `For ${childLabel}, the portal would center a short summary like: "Mood dropped after school conversation; one reassurance exchange; no severe escalation."`
          : `For ${childLabel}, the portal would keep the full transcript visible and attach safety notes directly beside the messages that triggered them.`;
      return {
        visible: true,
        message: visibilityCopy,
        caption: visibilityLabel(parentVisibilityMode),
      };
    }

    if (key === "alert_threshold") {
      if (!alertThreshold) {
        return { visible: false, message: "", caption: "" };
      }
      const thresholdCopy = {
        low: "Low threshold is the most sensitive, so the portal can surface early warning signs and lower-level changes sooner.",
        medium: "Medium threshold is the balanced default, so the portal highlights more meaningful concerns without surfacing every small fluctuation.",
        high: "High threshold is the least sensitive, so the portal mainly pushes forward stronger, clearer situations and keeps more minor signals in the background.",
      };
      const proactiveCopy = proactiveCheckIns
        ? `With proactive check-ins on, ${childLabel} could also get a gentle follow-up later if the conversation ends on a heavy note.`
        : `With proactive check-ins off, Resona would wait for ${childLabel} to initiate again instead of following up on its own.`;
      return {
        visible: true,
        message: `${thresholdCopy[alertThreshold] || thresholdCopy.medium} ${proactiveCopy}`,
        caption: `${humanize(alertThreshold)} threshold for parent-facing alerts`,
      };
    }

    if (key === "daily_cadence") {
      if (!dailyCadence) {
        return { visible: false, message: "", caption: "" };
      }
      const cadenceCopy = {
        adaptive: [
          `If ${childLabel} starts chatting at different times each day, Resona would follow that rhythm instead of forcing a fixed schedule.`,
          `Adaptive cadence lets Resona meet ${childLabel} where the week naturally opens up.`,
          `This keeps the timing flexible so conversations can happen when ${childLabel} is actually receptive.`,
          `Adaptive timing works best when ${childLabel}'s energy changes a lot from day to day.`,
          `Resona would learn the natural openings in ${childLabel}'s routine and stay responsive to those.`,
          `This keeps things from feeling overly scheduled if ${childLabel} responds differently each day.`,
        ],
        after_school: [
          `"Hey ${childName}, how did today feel once everything settled down?"`,
          `"Hi ${childName}, do you want to tell me one good part and one hard part from school?"`,
          `"Hey ${childName}, are you in the mood to decompress for a minute after school?"`,
          `"How was the day, ${childName}? We can do the short version first."`,
          `"Hi ${childName}, what stuck with you most from today?"`,
          `"Hey ${childName}, want a calm check-in now that the school day is over?"`,
        ],
        evening: [
          `"Hey ${childName}, before the day ends, what feels worth talking through?"`,
          `"Hi ${childName}, do you want a quieter end-of-day check-in tonight?"`,
          `"Before you settle in, ${childName}, what part of today is still on your mind?"`,
          `"Hey ${childName}, want to close out the day with a calm reset?"`,
          `"Tonight feels like a good time to slow down a little, ${childName}. Want to talk?"`,
          `"Hi ${childName}, do you want to sort through the day before quiet hours start?"`,
        ],
      };
      const voiceCopy = Boolean(readFieldValue("voice_enabled"))
        ? `If voice is included in the plan, the same tone can carry across calls with ${childLabel} too.`
        : `This setup stays text-first unless voice is enabled later for ${childLabel}.`;
      const quietHoursCopy =
        quietHoursStart && quietHoursEnd
          ? `Quiet hours will be respected from ${quietHoursStart} to ${quietHoursEnd}.`
          : "You can add quiet hours now or refine them later in the portal.";
      const cadenceMessage = pickVariant(
        cadenceCopy[dailyCadence] || cadenceCopy.adaptive,
        `${childName}|${dailyCadence}|cadence`
      );
      return {
        visible: true,
        message: `${cadenceMessage} ${voiceCopy}`,
        caption: quietHoursCopy,
      };
    }

    if (key === "selected_plan_key") {
      if (!selectedPlanKey) {
        return { visible: false, message: "", caption: "" };
      }
      const includedCredits = planMap.get(selectedPlanKey)?.included_credits_usd;
      return selectedPlanKey === "voice"
        ? {
            visible: true,
            message:
              `Resona Voice is the better fit if ${childLabel} responds well to hearing a familiar voice, or if you want calls and texts to feel like one continuous relationship.`,
            caption: `$${includedCredits || 30} in included monthly usage credits, then usage billing after credits are consumed.`,
          }
        : {
            visible: true,
            message:
              `Resona Chat is the better fit if texting is the main channel for ${childLabel}, you want a calmer monthly starting point, and voice can wait until you know the household wants it.`,
            caption: `$${includedCredits || 10} in included monthly usage credits, then usage billing after credits are consumed.`,
          };
    }

    return { visible: false, message: "", caption: "" };
  };

  const renderPreviews = () => {
    previewBoxes.forEach((box) => {
      const key = box.getAttribute("data-preview-box") || "";
      const content = buildPreviewContent(key);
      const messageNode = box.querySelector("[data-preview-message]");
      const captionNode = box.querySelector("[data-preview-caption]");
      box.hidden = !content.visible;
      if (!content.visible) {
        if (messageNode) {
          messageNode.textContent = "";
          messageNode.dataset.lastText = "";
        }
        if (captionNode) {
          captionNode.textContent = "";
        }
        return;
      }
      animatePreviewText(messageNode, content.message);
      if (captionNode) {
        captionNode.textContent = content.caption || "";
      }
    });
  };

  const listSummary = (values, customText, suffix) => {
    const items = [
      ...values.map((value) => humanize(value).toLowerCase()),
      ...(customText ? [customText] : []),
    ].filter(Boolean);
    if (items.length === 0) {
      return `flexible ${suffix}`;
    }
    if (items.length === 1) {
      return `${items[0]} ${suffix}`;
    }
    if (items.length === 2) {
      return `${items[0]} and ${items[1]} ${suffix}`;
    }
    return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]} ${suffix}`;
  };

  const collectPreferencePreviewPayload = () => ({
    profile_name: String(readFieldValue("profile_name") || snapshot.profile_name || "").trim(),
    preferred_pacing: Array.isArray(readFieldValue("preferred_pacing")) ? readFieldValue("preferred_pacing") : [],
    preferred_pacing_custom: String(readFieldValue("preferred_pacing_custom") || snapshot.preferred_pacing_custom || "").trim(),
    response_style: Array.isArray(readFieldValue("response_style")) ? readFieldValue("response_style") : [],
    response_style_custom: String(readFieldValue("response_style_custom") || snapshot.response_style_custom || "").trim(),
    communication_notes: String(readFieldValue("communication_notes") || snapshot.communication_notes || "").trim(),
    voice_enabled: Boolean(readFieldValue("voice_enabled")),
    proactive_check_ins: Boolean(readFieldValue("proactive_check_ins")),
    daily_cadence: String(readFieldValue("daily_cadence") || snapshot.daily_cadence || "").trim(),
  });

  const preferencePreviewKey = (previewPayload) => JSON.stringify(previewPayload);

  const setAiPreviewState = ({ message = "", caption = "", pending = false }) => {
    if (aiPreviewBox) {
      aiPreviewBox.dataset.state = pending ? "loading" : message ? "ready" : "idle";
    }
    if (aiPreviewMessage) {
      animatePreviewText(aiPreviewMessage, message);
    }
    if (aiPreviewCaption) {
      aiPreviewCaption.textContent = caption;
    }
  };

  const hasEnoughPreferencePreviewContext = (previewPayload) =>
    Boolean(previewPayload.profile_name) &&
    (previewPayload.preferred_pacing.length > 0 || previewPayload.preferred_pacing_custom) &&
    (previewPayload.response_style.length > 0 || previewPayload.response_style_custom);

  const currentPreferencePreviewKey = () => preferencePreviewKey(collectPreferencePreviewPayload());

  const loadingPreviewState = () => ({
    message: "Generating a new example...",
    caption: "Using your latest selections to write a fresh sample reply.",
    pending: true,
  });

  let lastResonaPresetKey = String(snapshot.resona_preset_key || "");

  const selectedResonaMode = () => String(readFieldValue("resona_mode") || snapshot.resona_mode || "preset").trim();
  const selectedResonaPresetKey = () => String(readFieldValue("resona_preset_key") || snapshot.resona_preset_key || "").trim();

  const syncResonaModeVisibility = () => {
    if (!resonaPresetPanel) {
      return;
    }
    resonaPresetPanel.hidden = selectedResonaMode() === "custom";
  };

  const maybeApplyPresetDefaults = () => {
    if (selectedResonaMode() === "custom") {
      syncResonaModeVisibility();
      return;
    }
    const nextPresetKey = selectedResonaPresetKey();
    const nextPreset = resonaPresetMap.get(nextPresetKey);
    if (!nextPreset) {
      syncResonaModeVisibility();
      return;
    }
    const previousPreset = resonaPresetMap.get(lastResonaPresetKey || "");
    const nameNodes = Array.from(form.querySelectorAll('[name="resona_display_name"]'));
    const nameNode = nameNodes[0];
    if (nameNode && (nameNode.value.trim() === "" || nameNode.value.trim() === String(previousPreset?.default_name || "").trim())) {
      nameNode.value = nextPreset.default_name || "";
    }
    const currentVoice = String(readFieldValue("resona_voice_profile_key") || "").trim();
    if (!currentVoice || currentVoice === String(previousPreset?.voice_profile_key || "").trim()) {
      syncFormValue("resona_voice_profile_key", nextPreset.voice_profile_key || "");
    }
    lastResonaPresetKey = nextPreset.key;
    syncResonaModeVisibility();
  };

  const collectResonaPreviewPayload = () => ({
    profile_name: String(readFieldValue("profile_name") || snapshot.profile_name || "").trim(),
    child_name: String(readFieldValue("profile_name") || snapshot.profile_name || "").trim(),
    resona_mode: selectedResonaMode(),
    resona_preset_key: selectedResonaPresetKey(),
    resona_display_name: String(readFieldValue("resona_display_name") || snapshot.resona_display_name || "").trim(),
    resona_voice_profile_key: String(readFieldValue("resona_voice_profile_key") || snapshot.resona_voice_profile_key || "").trim(),
    resona_vibe: String(readFieldValue("resona_vibe") || snapshot.resona_vibe || "").trim(),
    resona_support_style: String(readFieldValue("resona_support_style") || snapshot.resona_support_style || "").trim(),
    resona_avoid: String(readFieldValue("resona_avoid") || snapshot.resona_avoid || "").trim(),
    resona_anchors: String(readFieldValue("resona_anchors") || snapshot.resona_anchors || "").trim(),
    resona_proactive_style: String(readFieldValue("resona_proactive_style") || snapshot.resona_proactive_style || "").trim(),
  });

  const resonaPreviewKey = (previewPayload) => JSON.stringify(previewPayload);

  const setResonaPreviewState = ({ message = "", caption = "", pending = false, audioUrl = "" }) => {
    if (resonaPreviewButton) {
      resonaPreviewButton.disabled = pending;
      resonaPreviewButton.textContent = pending ? "Generating..." : "Preview Voice";
    }
    if (resonaPreviewMessage) {
      animatePreviewText(resonaPreviewMessage, message);
    }
    if (resonaPreviewCaption) {
      resonaPreviewCaption.textContent = caption;
    }
    if (resonaPreviewAudio) {
      if (audioUrl) {
        resonaPreviewAudio.src = audioUrl;
        resonaPreviewAudio.hidden = false;
      } else {
        resonaPreviewAudio.pause();
        resonaPreviewAudio.removeAttribute("src");
        resonaPreviewAudio.hidden = true;
      }
    }
  };

  const requestResonaPreview = async () => {
    const previewPayload = collectResonaPreviewPayload();
    const cacheKey = resonaPreviewKey(previewPayload);
    const cached = resonaPreviewCache.get(cacheKey);
    if (cached) {
      setResonaPreviewState(cached);
      return;
    }
    setResonaPreviewState({
      message: "Generating a short voice introduction...",
      caption: "Using the current Resona name and voice choice.",
      pending: true,
      audioUrl: "",
    });
    try {
      const result = await fetchJson(payload.resona_preview_url, {
        method: "POST",
        credentials: "same-origin",
        timeoutMs: 20000,
        resumeUrl: payload.resume_url || currentPathWithQuery(),
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          csrf_token: payload.csrf_token,
          ...previewPayload,
        }),
      });
      if (!result.ok) {
        if (result.code === RequestFailureCode.authExpired || result.data?.code === "auth_expired") {
          redirectToLogin(
            result.data?.login_url,
            "Your session expired before the voice preview could finish. Your setup draft is still safe in this tab."
          );
          return;
        }
        if (result.code === RequestFailureCode.offline) {
          setResonaPreviewState({
            message: "Preview unavailable while offline.",
            caption: "Reconnect and try again when you're ready.",
            pending: false,
            audioUrl: "",
          });
          return;
        }
        setResonaPreviewState({
          message: "Preview unavailable right now.",
          caption: result.data?.detail || "This environment could not generate a voice sample.",
          pending: false,
          audioUrl: "",
        });
        return;
      }
      const nextState = {
        message: result.data?.preview_text || "Voice sample ready.",
        caption: result.data?.voice_label || "Preview ready",
        pending: false,
        audioUrl: result.data?.audio_url || "",
      };
      resonaPreviewCache.set(cacheKey, nextState);
      setResonaPreviewState(nextState);
    } catch (error) {
      console.error("Resona preview failed", error);
      setResonaPreviewState({
        message: "Preview unavailable right now.",
        caption: "We hit a browser or network problem while generating the voice sample.",
        pending: false,
        audioUrl: "",
      });
    }
  };

  const schedulePreferencePreview = (previewPayload) => {
    if (previewRequestTimer) {
      window.clearTimeout(previewRequestTimer);
    }
    if (!hasEnoughPreferencePreviewContext(previewPayload)) {
      return;
    }
    const cacheKey = preferencePreviewKey(previewPayload);
    if (aiPreviewCache.has(cacheKey) || pendingPreviewKey === cacheKey) {
      return;
    }
    previewRequestTimer = window.setTimeout(() => {
      requestPreferencePreview(previewPayload);
    }, 720);
  };

  const refreshAiPreviewPrompt = () => {
    if (!aiPreviewBox) {
      return;
    }
    const previewPayload = collectPreferencePreviewPayload();
    if (!hasEnoughPreferencePreviewContext(previewPayload)) {
      if (previewRequestTimer) {
        window.clearTimeout(previewRequestTimer);
      }
      setAiPreviewState({
        message: "Choose a pacing, a tone, and add the child name above to generate a sample reply.",
        caption: "We only generate an example when there is enough context to make it useful.",
      });
      return;
    }
    const cacheKey = preferencePreviewKey(previewPayload);
    const cached = aiPreviewCache.get(cacheKey);
    if (cached) {
      setAiPreviewState({
        message: cached.message,
        caption: cached.caption,
      });
      return;
    }
    setAiPreviewState(loadingPreviewState());
    schedulePreferencePreview(previewPayload);
  };

  const requestPreferencePreview = async (inputPayload) => {
    const previewPayload = inputPayload || collectPreferencePreviewPayload();
    if (!hasEnoughPreferencePreviewContext(previewPayload)) {
      return;
    }
    const cacheKey = preferencePreviewKey(previewPayload);
    const cached = aiPreviewCache.get(cacheKey);
    if (cached) {
      setAiPreviewState({
        message: cached.message,
        caption: cached.caption,
      });
      return;
    }
    pendingPreviewKey = cacheKey;
    setAiPreviewState(loadingPreviewState());
    try {
      const runPreview = () =>
        fetchJson(payload.preview_url, {
          method: "POST",
          credentials: "same-origin",
          timeoutMs: 9000,
          resumeUrl: payload.resume_url || currentPathWithQuery(),
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            csrf_token: payload.csrf_token,
            data: previewPayload,
          }),
        });
      const result = await retryWithBackoff(runPreview, {
        retries: 1,
        baseDelayMs: 900,
        shouldRetry: (next) =>
          next.code === RequestFailureCode.timeout || next.code === RequestFailureCode.serverError,
      });
      if (!result.ok) {
        if (result.code === RequestFailureCode.authExpired || result.data?.code === "auth_expired") {
          redirectToLogin(
            result.data?.login_url,
            "Your session expired while we were generating an example. Your setup draft is still safe in this tab."
          );
          return;
        }
        if (result.code === RequestFailureCode.offline) {
          previewPendingOnReconnect = true;
          pendingPreviewPayload = previewPayload;
          if (currentPreferencePreviewKey() !== cacheKey) {
            return;
          }
          setAiPreviewState({
            message: "Preview unavailable while offline.",
            caption: "We’ll try again automatically when your connection returns.",
            pending: true,
          });
          return;
        }
        const data = result.data || {};
        const errorCaption =
          result.response?.status === 429
            ? "Preview limit reached for now."
            : data.detail || "Live AI wording is unavailable right now.";
        if (currentPreferencePreviewKey() !== cacheKey) {
          return;
        }
        setAiPreviewState({
          message: "Preview unavailable right now.",
          caption: errorCaption,
          pending: false,
        });
        return;
      }
      const data = result.data || {};
      previewPendingOnReconnect = false;
      pendingPreviewPayload = null;
      if (String(data.source || "") === "openai") {
        aiPreviewCache.set(cacheKey, { message: data.message, caption: data.caption });
      }
      if (currentPreferencePreviewKey() !== cacheKey) {
        return;
      }
      setAiPreviewState({
        message: data.message || "Preview unavailable right now.",
        caption: data.caption || data.detail || "Live AI wording is unavailable right now.",
        pending: false,
      });
    } catch (error) {
      console.error("Initialization preview failed", error);
      previewPendingOnReconnect = true;
      pendingPreviewPayload = previewPayload;
      if (currentPreferencePreviewKey() !== cacheKey) {
        return;
      }
      setAiPreviewState({
        message: "Trying again...",
        caption: "We’ll keep this example area warm and retry when the connection is steady again.",
        pending: true,
      });
    } finally {
      if (pendingPreviewKey === cacheKey) {
        pendingPreviewKey = "";
      }
    }
  };

  const clearErrors = () => {
    fieldErrors.forEach((node) => {
      node.textContent = "";
    });
  };

  const renderErrors = (errors) => {
    clearErrors();
    Object.entries(errors || {}).forEach(([field, message]) => {
      const node = form.querySelector(`[data-error-for="${field}"]`);
      if (node) {
        node.textContent = String(message || "");
      }
    });
  };

  const syncFormValue = (name, value) => {
    const nodes = Array.from(form.querySelectorAll(`[name="${name}"]`));
    if (nodes.length === 0) {
      return;
    }
    const first = nodes[0];
    if (first.type === "radio") {
      nodes.forEach((node) => {
        node.checked = node.value === String(value || "");
      });
      return;
    }
    if (first.type === "checkbox") {
      if (nodes.length > 1) {
        const selected = Array.isArray(value)
          ? value.map((item) => String(item))
          : value
            ? [String(value)]
            : [];
        nodes.forEach((node) => {
          node.checked = selected.includes(node.value);
        });
        return;
      }
      first.checked = Boolean(value);
      return;
    }
    first.value = value == null ? "" : String(value);
  };

  const syncForm = () => {
    Object.entries(snapshot || {}).forEach(([name, value]) => {
      syncFormValue(name, value);
    });
    syncResonaModeVisibility();
    maybeApplyPresetDefaults();
    renderPreviews();
    refreshAiPreviewPrompt();
  };

  const renderSummary = () => {
    summaryNodes.forEach((node) => {
      const field = node.getAttribute("data-summary-field") || "";
      node.textContent = summaryValue(field, summary[field]);
    });
    if (billingSelectedPlan) {
      billingSelectedPlan.textContent = planLabel(summary.selected_plan_key || snapshot.selected_plan_key);
    }
    if (billingSubscriptionStatus) {
      billingSubscriptionStatus.textContent = humanize(billingStatus || summary.subscription_status || "incomplete");
    }
  };

  const updateProgress = () => {
    indicators.forEach((indicator) => {
      const step = indicator.getAttribute("data-step-indicator") || "";
      indicator.classList.toggle("is-active", step === activeStep);
      indicator.classList.toggle("is-complete", completedSteps.has(step));
      indicator.disabled = !(step === activeStep || completedSteps.has(step));
    });
  };

  const setPanelVisibility = (panel, visible) => {
    panel.hidden = !visible;
    panel.classList.toggle("is-active", visible);
  };

  const renderStep = () => {
    const stepMeta = stepMap.get(activeStep);
    root.setAttribute("data-current-step", activeStep);
    if (currentMeta) {
      if (activeStep === "complete") {
        currentMeta.textContent = "Setup complete";
      } else {
        const visibleIndex = visibleSteps.findIndex((step) => step.key === activeStep);
        currentMeta.textContent =
          visibleIndex >= 0 ? `Step ${visibleIndex + 1} of ${visibleSteps.length}` : "Guided setup";
      }
    }
    if (currentTitle) {
      currentTitle.textContent = stepMeta?.label || humanize(activeStep);
    }
    if (currentDescription) {
      currentDescription.textContent =
        activeStep === "complete"
          ? "Everything is ready. You can head straight into the dashboard."
          : stepMeta?.description || "";
    }
    panels.forEach((panel) => {
      const step = panel.getAttribute("data-step-panel");
      setPanelVisibility(panel, step === activeStep);
    });
    updateProgress();
    renderPreviews();
    refreshAiPreviewPrompt();

    if (backButton) {
      backButton.hidden = activeStep === "welcome" || activeStep === "complete";
      backButton.disabled = inFlight || activeStep === "welcome" || activeStep === "complete";
    }
    if (nextButton) {
      nextButton.hidden = activeStep === "billing" || activeStep === "complete";
      nextButton.disabled = inFlight;
      nextButton.textContent = activeStep === "welcome" ? "Start setup" : activeStep === "plan" ? "Continue to billing" : "Next";
    }
  };

  const collectStepData = (step) => {
    const data = {};
    const fields = stepFields[step] || [];
    fields.forEach((name) => {
      const nodes = Array.from(form.querySelectorAll(`[name="${name}"]`));
      if (nodes.length === 0) {
        return;
      }
      data[name] = readFieldValue(name);
    });
    return data;
  };

  const applyServerState = (responsePayload, preserveStep) => {
    snapshot = responsePayload.snapshot || snapshot;
    summary = responsePayload.summary || summary;
    billingStatus = responsePayload.billing_status || billingStatus;
    payload.server_saved_at = responsePayload.server_saved_at || payload.server_saved_at || null;
    completedSteps = new Set(Array.isArray(responsePayload.completed_steps) ? responsePayload.completed_steps : []);
    syncForm();
    renderSummary();
    if (!preserveStep && responsePayload.current_step) {
      activeStep = responsePayload.current_step;
    }
    if (responsePayload.completion_ready) {
      clearStoredDraft();
    }
    renderStep();
  };

  const saveStep = async (step, mode) => {
    const isAutosave = mode === "autosave";
    if (inFlight && !isAutosave) {
      return { ok: false };
    }
    if (!isAutosave) {
      inFlight = true;
      renderStep();
    }
    persistDraft();
    setSaveState("saving", "Saving...");
    try {
      const runSave = () =>
        fetchJson(payload.save_url, {
          method: "POST",
          credentials: "same-origin",
          timeoutMs: 9000,
          resumeUrl: payload.resume_url || currentPathWithQuery(),
          headers: {
            "Content-Type": "application/json",
            "X-Resona-Step-Mode": mode,
          },
          body: JSON.stringify({
            step,
            data: collectStepData(step),
            csrf_token: payload.csrf_token,
          }),
        });
      const result = isAutosave
        ? await retryWithBackoff(runSave, {
            retries: 2,
            baseDelayMs: 800,
            shouldRetry: (next) =>
              next.code === RequestFailureCode.timeout || next.code === RequestFailureCode.serverError,
          })
        : await runSave();
      if (!result.ok) {
        const detail = result.data?.detail || "Unable to save setup right now.";
        if (result.code === RequestFailureCode.authExpired || result.data?.code === "auth_expired") {
          setSaveState("error", "Session expired");
          redirectToLogin(
            result.data?.login_url,
            "Your session expired while we were saving setup. Your changes are still safe in this tab."
          );
          return { ok: false, data: result.data };
        }
        if (result.code === RequestFailureCode.offline) {
          pendingAutosaveStep = step;
          setSaveState("error", "Waiting to reconnect");
          banner.show({
            tone: "warning",
            title: "Connection lost",
            message: "Your changes are safe in this tab and will resume saving when your connection returns.",
          });
          return { ok: false, data: result.data };
        }
        if (isAutosave) {
          pendingAutosaveStep = step;
          setSaveState("error", result.code === RequestFailureCode.timeout ? "Timed out" : "Save paused");
          banner.show({
            tone: "warning",
            title: "We’re still trying to save",
            message: "Your changes are safe in this tab. We’ll keep the latest draft here while the connection settles.",
          });
          return { ok: false, data: result.data };
        }
        renderErrors(result.data?.validation_errors || { [step]: detail });
        setSaveState("error", "Needs attention");
        return { ok: false, data: result.data };
      }
      const data = result.data || {};
      applyServerState(data, isAutosave);
      renderErrors(data.validation_errors || {});
      if (!data.ok) {
        setSaveState("error", "Needs attention");
        return { ok: false, data };
      }
      pendingAutosaveStep = "";
      lastSavedAt = data.server_saved_at ? new Date(data.server_saved_at) : new Date();
      persistDraft();
      setSaveState("saved", formatSavedAt(lastSavedAt));
      banner.hide();
      return { ok: true, data };
    } catch (error) {
      console.error("Initialization save failed", error);
      pendingAutosaveStep = step;
      setSaveState("error", "Save failed");
      banner.show({
        tone: "warning",
        title: "We’re still trying to save",
        message: "Your changes are safe in this tab, even though the last save did not finish.",
      });
      return { ok: false };
    } finally {
      if (!isAutosave) {
        inFlight = false;
        renderStep();
      }
    }
  };

  const scheduleAutosave = () => {
    if (!["household", "child", "resona", "preferences", "plan"].includes(activeStep)) {
      return;
    }
    persistDraft();
    if (autosaveTimer) {
      window.clearTimeout(autosaveTimer);
    }
    autosaveTimer = window.setTimeout(() => {
      saveStep(activeStep, "autosave");
    }, 420);
  };

  const setActiveStep = async (step, persistCurrent) => {
    if (persistCurrent && ["household", "child", "resona", "preferences", "plan"].includes(activeStep)) {
      await saveStep(activeStep, "autosave");
    }
    activeStep = step;
    clearErrors();
    renderStep();
  };

  form.addEventListener("input", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    if (target.matches("input, textarea")) {
      if (target.getAttribute("name") === "resona_display_name") {
        lastResonaPresetKey = selectedResonaPresetKey();
      }
      renderPreviews();
      refreshAiPreviewPrompt();
      scheduleAutosave();
    }
  });

  form.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    if (target.matches("select, input[type='checkbox'], input[type='radio']")) {
      const fieldName = target.getAttribute("name") || "";
      if (fieldName === "resona_mode") {
        syncResonaModeVisibility();
      }
      if (fieldName === "resona_preset_key" || fieldName === "resona_mode") {
        maybeApplyPresetDefaults();
      }
      renderPreviews();
      refreshAiPreviewPrompt();
      scheduleAutosave();
    }
  });

  if (resonaPreviewButton) {
    resonaPreviewButton.addEventListener("click", () => {
      requestResonaPreview();
    });
  }

  indicators.forEach((indicator) => {
    indicator.addEventListener("click", async () => {
      const targetStep = indicator.getAttribute("data-step-jump") || "";
      if (!targetStep || targetStep === activeStep || indicator.disabled) {
        return;
      }
      await setActiveStep(targetStep, true);
    });
  });

  if (backButton) {
    backButton.addEventListener("click", async () => {
      const index = stepOrder.indexOf(activeStep);
      const previousStep = stepOrder[Math.max(index - 1, 0)] || "welcome";
      if (previousStep === activeStep) {
        return;
      }
      await setActiveStep(previousStep, true);
    });
  }

  if (nextButton) {
    nextButton.addEventListener("click", async () => {
      const result = await saveStep(activeStep, "advance");
      if (result.ok && result.data?.current_step) {
        activeStep = result.data.current_step;
        renderStep();
      }
    });
  }

  if (billingButton) {
    billingButton.addEventListener("click", async () => {
      if (billingInFlight) {
        return;
      }
      billingInFlight = true;
      billingButton.disabled = true;
      persistDraft();
      setSaveState("saving", "Redirecting");
      try {
        const selectedPlanKey =
          collectStepData("plan").selected_plan_key ||
          snapshot.selected_plan_key ||
          summary.selected_plan_key ||
          "";
        const result = await fetchJson(payload.billing_url, {
          method: "POST",
          credentials: "same-origin",
          timeoutMs: 10000,
          resumeUrl: payload.resume_url || currentPathWithQuery(),
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            csrf_token: payload.csrf_token,
            selected_plan_key: selectedPlanKey,
          }),
        });
        if (!result.ok) {
          if (result.code === RequestFailureCode.authExpired || result.data?.code === "auth_expired") {
            setSaveState("error", "Session expired");
            redirectToLogin(
              result.data?.login_url,
              "Your session expired before checkout started. Your setup draft is still safe in this tab."
            );
            return;
          }
          if (result.code === RequestFailureCode.offline || result.code === RequestFailureCode.timeout) {
            banner.show({
              tone: "warning",
              title: "No new checkout was started",
              message: "Your connection dropped before Stripe checkout could begin.",
            });
            renderErrors({ billing: "No new checkout was started. Check your connection and try again." });
            setSaveState("error", "Checkout paused");
            return;
          }
          banner.show({
            tone: "warning",
            title: "No new checkout was started",
            message: result.data?.detail || "We couldn’t start Stripe checkout just yet.",
          });
          renderErrors(result.data?.validation_errors || { billing: result.data?.detail || "Unable to start checkout." });
          setSaveState("error", "Needs attention");
          return;
        }
        const data = result.data || {};
        if (data.already_active) {
          clearStoredDraft();
        }
        window.location.href = data.url;
      } catch (error) {
        console.error("Billing checkout failed", error);
        banner.show({
          tone: "warning",
          title: "No new checkout was started",
          message: "We hit a browser or network problem before Stripe checkout could open.",
        });
        renderErrors({ billing: "Unable to start checkout right now." });
        setSaveState("error", "Checkout failed");
      } finally {
        billingInFlight = false;
        billingButton.disabled = false;
      }
    });
  }

  window.addEventListener("resona:auth-expired", () => {
    persistDraft();
  });
  window.addEventListener("beforeunload", () => {
    persistDraft();
  });

  watchConnectivity({
    immediate: false,
    onOffline: () => {
      banner.show({
        tone: "warning",
        title: "Connection lost",
        message: "Your changes are safe in this tab while the connection is down.",
      });
    },
    onOnline: () => {
      if (pendingAutosaveStep) {
        banner.show({
          tone: "success",
          title: "Connection restored",
          message: "We’re saving the latest setup draft again now.",
        });
        const stepToRetry = pendingAutosaveStep;
        pendingAutosaveStep = "";
        saveStep(stepToRetry, "autosave");
        return;
      }
      if (previewPendingOnReconnect && pendingPreviewPayload) {
        banner.show({
          tone: "success",
          title: "Connection restored",
          message: "We’re generating the example reply again now.",
        });
        previewPendingOnReconnect = false;
        const previewPayload = pendingPreviewPayload;
        pendingPreviewPayload = null;
        requestPreferencePreview(previewPayload);
        return;
      }
      banner.hide();
    },
  });

  restoreStoredDraftIfNewer();
  syncForm();
  renderSummary();
  renderStep();
  setSaveState("saved", formatSavedAt(lastSavedAt));
})();
