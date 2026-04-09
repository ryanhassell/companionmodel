import { RequestFailureCode, createPortalBanner, fetchJson, publishSessionEvent, retryWithBackoff, watchConnectivity } from "/static/portal-resilience.js";
import { clerkDisplayName, clerkPrimaryEmail, loadClerkClient } from "/static/portal-clerk.js";

const payloadNode = document.getElementById("portal-callback-data");
const statusNode = document.getElementById("clerk-sync-status");
const actionsNode = document.getElementById("portal-callback-actions");
const retryButton = document.getElementById("portal-callback-retry");
const backLink = document.getElementById("portal-callback-back");
const banner = createPortalBanner();

if (!payloadNode) {
  throw new Error("Missing portal callback payload");
}

const payload = JSON.parse(payloadNode.textContent || "{}");
const nextPath = payload.next_path || "/app/landing";
const signInUrl = payload.sign_in_url || "/app/login?reason=invalid_session";

let syncInFlight = false;
let pendingRetryOnReconnect = false;

const setStatus = (message) => {
  if (statusNode) {
    statusNode.textContent = message;
  }
};

const showActions = () => {
  if (actionsNode) {
    actionsNode.hidden = false;
  }
  if (backLink) {
    backLink.href = signInUrl;
  }
};

const hideActions = () => {
  if (actionsNode) {
    actionsNode.hidden = true;
  }
};

const handleSyncFailure = (result) => {
  if (result.code === RequestFailureCode.authExpired || result.data?.code === "invalid_session") {
    publishSessionEvent("session_expired", { path: nextPath });
    banner.show({
      tone: "danger",
      title: "We couldn’t finish secure sign-in",
      message: "Your Clerk session needs to be refreshed before we can reopen the parent portal.",
      actions: [{ label: "Back to sign in", href: signInUrl, variant: "ghost" }],
    });
    setStatus("Your secure session expired before we could finish linking the portal.");
    showActions();
    return;
  }

  if (result.code === RequestFailureCode.offline) {
    banner.show({
      tone: "warning",
      title: "Connection lost",
      message: "We’ll keep this page ready and resume once your connection returns.",
    });
    setStatus("Waiting for your connection to come back...");
    pendingRetryOnReconnect = true;
    showActions();
    return;
  }

  if (result.code === RequestFailureCode.timeout) {
    banner.show({
      tone: "warning",
      title: "Still trying to finish sign-in",
      message: "The portal handoff took too long. You can retry here safely.",
      actions: [{ label: "Retry sign-in", onClick: () => startSync(), variant: "ghost" }],
    });
    setStatus("The secure handoff timed out before it finished.");
    showActions();
    return;
  }

  banner.show({
    tone: "danger",
    title: "We couldn’t finish secure sign-in",
    message: "The portal handoff hit a server problem. You can retry here or return to sign in.",
    actions: [{ label: "Retry sign-in", onClick: () => startSync(), variant: "ghost" }],
  });
  setStatus("The parent portal couldn’t finish linking your session just yet.");
  showActions();
};

const startSync = async () => {
  if (syncInFlight) {
    return;
  }
  syncInFlight = true;
  hideActions();

  if (window.navigator.onLine === false) {
    handleSyncFailure({ code: RequestFailureCode.offline });
    syncInFlight = false;
    return;
  }

  try {
    setStatus("Checking your secure Clerk session…");
    const clerk = await loadClerkClient({
      publishableKey: document.body?.dataset.clerkPublishableKey || "",
      frontendApiUrl: document.body?.dataset.clerkFrontendApiUrl || "",
      includeUi: false,
    });

    if (!clerk.session) {
      handleSyncFailure({ code: RequestFailureCode.authExpired, data: { code: "invalid_session" } });
      return;
    }

    const token = await clerk.session.getToken();
    const runSync = async () => {
      setStatus("Securing your portal session…");
      return fetchJson("/app/auth/sync", {
        method: "POST",
        credentials: "same-origin",
        timeoutMs: 9000,
        resumeUrl: nextPath,
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          token: token || "",
          email: clerkPrimaryEmail(clerk),
          display_name: clerkDisplayName(clerk),
        }),
      });
    };

    const result = await retryWithBackoff(runSync, {
      retries: 2,
      baseDelayMs: 900,
      shouldRetry: (next) => next.code === RequestFailureCode.offline || next.code === RequestFailureCode.timeout || (next.code === RequestFailureCode.serverError && next.retryable),
      onRetry: (next, attempt) => {
        if (next.code === RequestFailureCode.offline) {
          setStatus("Connection lost. Waiting to retry sign-in…");
        } else {
          setStatus(`Trying again (${attempt + 1}/3)…`);
        }
      },
    });

    if (!result.ok) {
      handleSyncFailure(result);
      return;
    }

    pendingRetryOnReconnect = false;
    banner.hide();
    setStatus("Secure handoff complete. Opening your portal…");
    window.location.replace(nextPath);
  } catch (error) {
    console.error("Clerk callback failed", error);
    handleSyncFailure({ code: RequestFailureCode.serverError, retryable: true });
  } finally {
    syncInFlight = false;
  }
};

watchConnectivity({
  immediate: false,
  onOnline: () => {
    if (!pendingRetryOnReconnect) {
      return;
    }
    pendingRetryOnReconnect = false;
    banner.show({
      tone: "success",
      title: "Connection restored",
      message: "We’re trying the portal handoff again now.",
    });
    startSync();
  },
  onOffline: () => {
    banner.show({
      tone: "warning",
      title: "Connection lost",
      message: "We’ll keep your sign-in state here and resume once you’re back online.",
    });
  },
});

retryButton?.addEventListener("click", () => {
  startSync();
});

window.addEventListener("load", () => {
  startSync();
});
