import "https://esm.sh/@clerk/ui@1.5.0/register";

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
  options: {
    termsPageUrl: "/terms-and-conditions",
    privacyPageUrl: "/privacy-policy",
  },
};

const statusCopy = {
  "sign-in": {
    preparing: "Preparing secure sign-in…",
    signedOut: "Ending your previous session…",
    redirecting: "Finishing secure sign-in…",
    mounted: "If you use 2-step verification, keep going here.",
    failure: "We couldn't load the sign-in form.",
  },
  "sign-up": {
    preparing: "Preparing secure account creation…",
    redirecting: "Finishing account setup…",
    mounted: "Use the form here to create your account.",
    failure: "We couldn't load account creation right now.",
  },
};

const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

const waitForClerk = () =>
  new Promise((resolve, reject) => {
    let attempts = 0;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (window.Clerk) {
        window.clearInterval(timer);
        resolve(window.Clerk);
        return;
      }
      if (attempts > 120) {
        window.clearInterval(timer);
        reject(new Error("Clerk script did not load"));
      }
    }, 50);
  });

const loadSharedUiBundle = () =>
  new Promise((resolve, reject) => {
    if (window.__internal_ClerkUICtor) {
      resolve(window.__internal_ClerkUICtor);
      return;
    }

    const existing = document.querySelector('script[data-clerk-ui-shared="true"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(window.__internal_ClerkUICtor), { once: true });
      existing.addEventListener("error", () => reject(new Error("Clerk UI bundle failed to load")), { once: true });
      return;
    }

    const script = document.createElement("script");
    script.async = true;
    script.dataset.clerkUiShared = "true";
    script.src = "https://cdn.jsdelivr.net/npm/@clerk/ui@1.5.0/dist/ui.shared.browser.js";
    script.onload = () => resolve(window.__internal_ClerkUICtor);
    script.onerror = () => reject(new Error("Clerk UI bundle failed to load"));
    document.head.appendChild(script);
  });

const renderFailure = (shell, message) => {
  if (!shell) {
    return;
  }
  shell.innerHTML = [
    `<p class="pill" style="background:#fff1ee;color:#8a3428;">${message}</p>`,
    '<p class="subtle">Please refresh once and try again.</p>',
  ].join("");
};

const syncExistingSession = (clerk, callbackUrl, statusNode, mode) => {
  if (!clerk.user || !clerk.session) {
    return false;
  }
  if (statusNode) {
    statusNode.textContent = statusCopy[mode].redirecting;
  }
  window.location.replace(callbackUrl);
  return true;
};

const mountAuthComponent = (clerk, mode, mountNode, alternateUrl, callbackUrl) => {
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
    return;
  }

  clerk.mountSignUp(mountNode, {
    ...common,
    signInUrl: alternateUrl,
  });
};

window.addEventListener("load", async () => {
  const mountNode = document.querySelector("[data-clerk-auth-page]");
  if (!mountNode) {
    return;
  }

  const body = document.body;
  const shell = mountNode.closest(".clerk-auth-shell");
  const statusNode = document.getElementById(mountNode.dataset.clerkStatusId || "");
  const mode = mountNode.dataset.clerkAuthPage || "sign-in";
  const callbackUrl = mountNode.dataset.clerkCallbackUrl || "/app/session/callback?next=/app/landing";
  const alternateUrl = mountNode.dataset.clerkAlternateUrl || (mode === "sign-in" ? "/app/signup" : "/app/login");
  const signedOutRequested =
    mode === "sign-in" && new URLSearchParams(window.location.search).get("signed_out") === "1";

  try {
    if (statusNode) {
      statusNode.textContent = statusCopy[mode].preparing;
    }

    await loadSharedUiBundle();
    const clerk = await waitForClerk();
    await clerk.load({
      publishableKey: body?.dataset.clerkPublishableKey || "",
      ...(body?.dataset.clerkFrontendApiUrl
        ? { frontendApi: body.dataset.clerkFrontendApiUrl }
        : {}),
      clerkUICtor: window.__internal_ClerkUICtor,
    });

    if (signedOutRequested && clerk.session) {
      if (statusNode) {
        statusNode.textContent = statusCopy[mode].signedOut;
      }
      try {
        await Promise.race([
          clerk.signOut({ redirectUrl: "/app/login?logged_out=1" }),
          wait(1500),
        ]);
      } catch {
        // Keep going into the sign-in flow if Clerk sign-out stalls.
      }
    }

    if (!signedOutRequested && syncExistingSession(clerk, callbackUrl, statusNode, mode)) {
      return;
    }

    if (!mountNode.id) {
      throw new Error("Missing Clerk auth mount node id");
    }

    mountAuthComponent(clerk, mode, mountNode, alternateUrl, callbackUrl);
    if (statusNode) {
      statusNode.textContent = statusCopy[mode].mounted;
    }
  } catch (error) {
    console.error(`Clerk ${mode} mount failed`, error);
    renderFailure(shell, statusCopy[mode].failure);
  }
});
