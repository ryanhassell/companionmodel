const payloadNode = document.getElementById("portal-child-resona-data");

if (payloadNode) {
  let payload = {};
  try {
    payload = JSON.parse(payloadNode.textContent || "{}");
  } catch {
    payload = {};
  }

  const form = document.querySelector("[data-child-resona-form]");
  const previewButton = document.getElementById("child-resona-preview-button");
  const previewMessage = document.getElementById("child-resona-preview-message");
  const previewCaption = document.getElementById("child-resona-preview-caption");
  const previewAudio = document.getElementById("child-resona-preview-audio");
  const presetPanel = document.querySelector("[data-child-resona-preset-panel]");
  const presetMap = new Map((Array.isArray(payload.preset_options) ? payload.preset_options : []).map((item) => [item.key, item]));
  const previewCache = new Map();
  let lastPresetKey = String(payload.resona_preset_key || "");

  if (form) {
    const readFieldValue = (name) => {
      const nodes = Array.from(form.querySelectorAll(`[name="${name}"]`));
      if (!nodes.length) {
        return "";
      }
      const [first] = nodes;
      if (first.type === "radio") {
        return (nodes.find((node) => node.checked) || first).value || "";
      }
      return first.value || "";
    };

    const setFieldValue = (name, value) => {
      const nodes = Array.from(form.querySelectorAll(`[name="${name}"]`));
      if (!nodes.length) {
        return;
      }
      if (nodes[0].type === "radio") {
        nodes.forEach((node) => {
          node.checked = node.value === String(value || "");
        });
        return;
      }
      nodes[0].value = value == null ? "" : String(value);
    };

    const selectedMode = () => String(readFieldValue("resona_mode") || "preset").trim();
    const selectedPresetKey = () => String(readFieldValue("resona_preset_key") || "").trim();

    const syncPresetVisibility = () => {
      if (presetPanel) {
        presetPanel.hidden = selectedMode() === "custom";
      }
    };

    const maybeApplyPresetDefaults = () => {
      if (selectedMode() === "custom") {
        syncPresetVisibility();
        return;
      }
      const nextPreset = presetMap.get(selectedPresetKey());
      if (!nextPreset) {
        syncPresetVisibility();
        return;
      }
      const previousPreset = presetMap.get(lastPresetKey || "");
      const currentName = String(readFieldValue("resona_display_name") || "").trim();
      if (!currentName || currentName === String(previousPreset?.default_name || "").trim()) {
        setFieldValue("resona_display_name", nextPreset.default_name || "");
      }
      const currentVoice = String(readFieldValue("resona_voice_profile_key") || "").trim();
      if (!currentVoice || currentVoice === String(previousPreset?.voice_profile_key || "").trim()) {
        setFieldValue("resona_voice_profile_key", nextPreset.voice_profile_key || "");
      }
      lastPresetKey = nextPreset.key;
      syncPresetVisibility();
    };

    const collectPreviewPayload = () => ({
      csrf_token: payload.csrf_token,
      resona_mode: selectedMode(),
      resona_preset_key: selectedPresetKey(),
      resona_display_name: String(readFieldValue("resona_display_name") || "").trim(),
      resona_voice_profile_key: String(readFieldValue("resona_voice_profile_key") || "").trim(),
      resona_vibe: String(readFieldValue("resona_vibe") || "").trim(),
      resona_support_style: String(readFieldValue("resona_support_style") || "").trim(),
      resona_avoid: String(readFieldValue("resona_avoid") || "").trim(),
      resona_anchors: String(readFieldValue("resona_anchors") || "").trim(),
      resona_proactive_style: String(readFieldValue("resona_proactive_style") || "").trim(),
    });

    const setPreviewState = ({ message = "", caption = "", audioUrl = "", pending = false }) => {
      if (previewButton) {
        previewButton.disabled = pending;
        previewButton.textContent = pending ? "Generating..." : "Preview Voice";
      }
      if (previewMessage) {
        previewMessage.textContent = message;
      }
      if (previewCaption) {
        previewCaption.textContent = caption;
      }
      if (previewAudio) {
        if (audioUrl) {
          previewAudio.src = audioUrl;
          previewAudio.hidden = false;
        } else {
          previewAudio.pause();
          previewAudio.removeAttribute("src");
          previewAudio.hidden = true;
        }
      }
    };

    const requestPreview = async () => {
      if (!payload.preview_url) {
        return;
      }
      const requestPayload = collectPreviewPayload();
      const cacheKey = JSON.stringify(requestPayload);
      const cached = previewCache.get(cacheKey);
      if (cached) {
        setPreviewState(cached);
        return;
      }
      setPreviewState({
        message: "Generating a short voice introduction...",
        caption: "Using the current Resona name and voice selection.",
        pending: true,
        audioUrl: "",
      });
      try {
        const response = await fetch(payload.preview_url, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-Resona-Request": "fetch",
          },
          body: JSON.stringify(requestPayload),
        });
        const data = await response.json();
        if (!response.ok || data.ok === false) {
          setPreviewState({
            message: "Preview unavailable right now.",
            caption: data.detail || "We couldn't generate a voice sample in this environment.",
            pending: false,
            audioUrl: "",
          });
          return;
        }
        const nextState = {
          message: data.preview_text || "Voice sample ready.",
          caption: data.voice_label || "Preview ready",
          pending: false,
          audioUrl: data.audio_url || "",
        };
        previewCache.set(cacheKey, nextState);
        setPreviewState(nextState);
      } catch {
        setPreviewState({
          message: "Preview unavailable right now.",
          caption: "We hit a browser or network problem while generating the voice sample.",
          pending: false,
          audioUrl: "",
        });
      }
    };

    form.addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) {
        return;
      }
      const fieldName = target.getAttribute("name") || "";
      if (fieldName === "resona_mode") {
        syncPresetVisibility();
      }
      if (fieldName === "resona_preset_key" || fieldName === "resona_mode") {
        maybeApplyPresetDefaults();
      }
    });

    if (previewButton) {
      previewButton.addEventListener("click", () => {
        requestPreview();
      });
    }

    syncPresetVisibility();
    maybeApplyPresetDefaults();
  }
}
