import { createPortalBanner, wait, watchConnectivity } from "/static/portal-resilience.js";
import { loadClerkClient } from "/static/portal-clerk.js";

const APPEARANCE = {
  variables: {
    colorPrimary: "#1e5f53",
    colorText: "#222827",
    colorTextSecondary: "#67706d",
    colorBackground: "#fcfaf5",
    colorInputBackground: "#ffffff",
    colorInputText: "#222827",
    borderRadius: "0px",
    fontFamily: '"DM Sans", "Avenir Next", "Segoe UI", sans-serif',
  },
  elements: {
    rootBox: {
      width: "100%",
      maxWidth: "100%",
    },
    cardBox: {
      width: "100%",
      maxWidth: "100%",
      margin: "0",
    },
    main: {
      width: "100%",
      maxWidth: "100%",
    },
    page: {
      width: "100%",
      maxWidth: "100%",
    },
    card: {
      width: "100%",
      maxWidth: "100%",
      margin: "0",
      borderRadius: "0px",
    },
    modalContent: {
      width: "min(960px, calc(100vw - 40px))",
      maxWidth: "960px",
      borderRadius: "0px",
    },
  },
  options: {
    termsPageUrl: "/terms-and-conditions",
    privacyPageUrl: "/privacy-policy",
  },
};

const statusCopy = {
  "sign-in": {
    preparing: "Preparing secure sign-in...",
    signedOut: "Ending your previous session...",
    mounted: "If you use 2-step verification, keep going here.",
    redirecting: "Finishing secure sign-in...",
    offline: "Waiting for your connection before we load the sign-in form.",
    failure: "We couldn't load the sign-in form.",
    reconnecting: "Connection restored. Reloading secure sign-in...",
  },
  "sign-up": {
    preparing: "Preparing secure account creation...",
    mounted: "Use the form here to create your account.",
    redirecting: "Finishing account setup...",
    offline: "Waiting for your connection before we load account creation.",
    failure: "We couldn't load account creation right now.",
    reconnecting: "Connection restored. Reloading secure account creation...",
  },
};

const renderRecoveryState = (shell, { title, message, actions = [] }) => {
  if (!shell) {
    return null;
  }
  const existing = shell.querySelector("[data-auth-recovery]");
  existing?.remove();

  const card = document.createElement("div");
  card.className = "card auth-launch-card";
  card.dataset.authRecovery = "true";

  const heading = document.createElement("h3");
  heading.textContent = title;
  card.appendChild(heading);

  const copy = document.createElement("p");
  copy.className = "subtle";
  copy.textContent = message;
  card.appendChild(copy);

  if (actions.length > 0) {
    const actionsRow = document.createElement("div");
    actionsRow.className = "auth-launch-actions";
    actions.forEach((action) => {
      const node = action.href ? document.createElement("a") : document.createElement("button");
      if (action.href) {
        node.href = action.href;
      } else {
        node.type = "button";
        node.addEventListener("click", () => action.onClick?.());
      }
      node.className = action.variant === "ghost" ? "button ghost" : "button secondary";
      node.textContent = action.label;
      actionsRow.appendChild(node);
    });
    card.appendChild(actionsRow);
  }

  shell.appendChild(card);
  return card;
};

const initAuthPage = (mountNode) => {
  const banner = createPortalBanner();
  const shell = mountNode.closest(".clerk-auth-shell");
  const statusNode = document.getElementById(mountNode.dataset.clerkStatusId || "");
  const mode = mountNode.dataset.clerkAuthPage || "sign-in";
  const callbackUrl = mountNode.dataset.clerkCallbackUrl || "/app/session/callback?next=/app/landing";
  const alternateUrl =
    mountNode.dataset.clerkAlternateUrl || (mode === "sign-in" ? "/app/signup" : "/app/login");
  const signedOutRequested =
    mode === "sign-in" && new URLSearchParams(window.location.search).get("signed_out") === "1";

  let recoveryNode = null;
  let mountInFlight = false;
  let redirectInFlight = false;
  let mounted = false;
  let waitingForReconnect = false;

  const setStatus = (message) => {
    if (statusNode) {
      statusNode.textContent = message;
    }
  };

  const clearRecoveryState = () => {
    recoveryNode?.remove();
    recoveryNode = null;
    mountNode.hidden = false;
  };

  const showRecoveryState = (title, message, actions) => {
    mountNode.hidden = true;
    recoveryNode = renderRecoveryState(shell, { title, message, actions });
  };

  const redirectToCallback = () => {
    if (redirectInFlight) {
      return;
    }
    redirectInFlight = true;
    setStatus(statusCopy[mode].redirecting);
    window.location.replace(callbackUrl);
  };

  const mountComponent = async () => {
    if (mountInFlight || redirectInFlight || mounted) {
      return;
    }

    if (window.navigator.onLine === false) {
      waitingForReconnect = true;
      setStatus(statusCopy[mode].offline);
      banner.show({
        tone: "warning",
        title: "Waiting for connection",
        message: "We’ll load secure sign-in as soon as your connection returns.",
      });
      showRecoveryState("Connection lost", "Your sign-in page is waiting for the network to come back.", [
        { label: "Reload page", onClick: () => window.location.reload(), variant: "ghost" },
      ]);
      return;
    }

    mountInFlight = true;
    clearRecoveryState();
    banner.hide();
    setStatus(statusCopy[mode].preparing);

    try {
      const clerk = await loadClerkClient({
        publishableKey: document.body?.dataset.clerkPublishableKey || "",
        frontendApiUrl: document.body?.dataset.clerkFrontendApiUrl || "",
        includeUi: true,
      });

      if (mode === "sign-in" && signedOutRequested && clerk.session) {
        setStatus(statusCopy[mode].signedOut);
        try {
          await Promise.race([clerk.signOut({ redirectUrl: "/app/login?logged_out=1" }), wait(1500)]);
        } catch {
          // If Clerk sign-out stalls, continue into the sign-in UI instead of trapping the page.
        }
      }

      if (!signedOutRequested && clerk.session && clerk.user) {
        redirectToCallback();
        return;
      }

      try {
        if (mode === "sign-in") {
          clerk.unmountSignIn?.(mountNode);
        } else {
          clerk.unmountSignUp?.(mountNode);
        }
      } catch {
        // Ignore unmount mismatches while reloading the embedded auth UI.
      }
      mountNode.replaceChildren();

      const common = {
        routing: "hash",
        forceRedirectUrl: callbackUrl,
        fallbackRedirectUrl: callbackUrl,
        appearance: APPEARANCE,
      };
      if (mode === "sign-in") {
        clerk.mountSignIn(mountNode, {
          ...common,
          signUpUrl: alternateUrl,
        });
      } else {
        clerk.mountSignUp(mountNode, {
          ...common,
          signInUrl: alternateUrl,
        });
      }

      mounted = true;
      waitingForReconnect = false;
      setStatus(statusCopy[mode].mounted);
      banner.hide();
    } catch (error) {
      console.error(`Clerk ${mode} mount failed`, error);
      waitingForReconnect = window.navigator.onLine === false;
      const isOffline = waitingForReconnect;
      const message = isOffline
        ? "We’ll resume loading this form automatically when your connection comes back."
        : "The secure sign-in UI could not finish loading. You can retry without leaving the page.";

      setStatus(statusCopy[mode].failure);
      banner.show({
        tone: isOffline ? "warning" : "danger",
        title: isOffline ? "Waiting for connection" : "We couldn’t finish secure sign-in",
        message,
        actions: [
          { label: "Retry loading form", onClick: () => mountComponent(), variant: "ghost" },
          { label: "Reload page", onClick: () => window.location.reload(), variant: "ghost" },
        ],
      });
      showRecoveryState(
        isOffline ? "Waiting for connection" : "We couldn’t load the secure form",
        message,
        [
          { label: "Retry loading form", onClick: () => mountComponent(), variant: "ghost" },
          { label: "Reload page", onClick: () => window.location.reload(), variant: "ghost" },
        ]
      );
    } finally {
      mountInFlight = false;
    }
  };

  watchConnectivity({
    immediate: true,
    onOffline: () => {
      if (mounted) {
        banner.show({
          tone: "warning",
          title: "Connection lost",
          message: "Your auth step stays here. As soon as the connection returns, Clerk can continue where you left off.",
        });
        return;
      }
      waitingForReconnect = true;
      setStatus(statusCopy[mode].offline);
    },
    onOnline: () => {
      if (!waitingForReconnect || mounted || redirectInFlight) {
        if (mounted) {
          banner.hide();
        }
        return;
      }
      waitingForReconnect = false;
      setStatus(statusCopy[mode].reconnecting);
      banner.show({
        tone: "success",
        title: "Connection restored",
        message: "We’re loading the secure form again now.",
      });
      mountComponent();
    },
  });

  mountComponent();
};

window.addEventListener("load", () => {
  const mountNode = document.querySelector("[data-clerk-auth-page]");
  if (!mountNode) {
    return;
  }
  initAuthPage(mountNode);
});
