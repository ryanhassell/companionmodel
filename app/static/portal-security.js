import { createPortalBanner } from "/static/portal-resilience.js";
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
    pageScrollBox: {
      width: "100%",
      maxWidth: "100%",
    },
    scrollBox: {
      width: "100%",
      maxWidth: "100%",
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

const root = document.querySelector("[data-clerk-user-profile-root]");
const banner = createPortalBanner();

const showRetry = (message) => {
  banner.show({
    tone: "danger",
    title: "Security controls are unavailable",
    message,
    actions: [
      {
        label: "Retry loading security",
        onClick: () => window.location.reload(),
        variant: "ghost",
      },
    ],
  });
};

const mountUserProfile = async () => {
  if (!root) {
    return;
  }
  try {
    const clerk = await loadClerkClient({
      publishableKey: document.body?.dataset.clerkPublishableKey || "",
      frontendApiUrl: document.body?.dataset.clerkFrontendApiUrl || "",
      includeUi: true,
    });

    if (!clerk.session || !clerk.user) {
      banner.show({
        tone: "warning",
        title: "Session expired",
        message: "Please sign in again before opening account security.",
        actions: [
          { label: "Back to sign in", href: "/app/login?reason=invalid_session", variant: "ghost" },
        ],
      });
      return;
    }

    try {
      clerk.unmountUserProfile?.(root);
    } catch {
      // Ignore stale mount state during a fresh page load.
    }
    root.replaceChildren();
    clerk.mountUserProfile(root, {
      routing: "hash",
      appearance: APPEARANCE,
    });
    banner.hide();
  } catch (error) {
    console.error("Clerk user profile mount failed", error);
    const isOffline = window.navigator.onLine === false;
    showRetry(
      isOffline
        ? "Your connection dropped before Clerk could finish loading. Reconnect, then try again."
        : "Clerk account security could not finish loading. Refresh the page to try again."
    );
  }
};

mountUserProfile();
