import { createPortalBanner, currentPathWithQuery, publishSessionEvent, subscribeSessionEvents, wait } from "/static/portal-resilience.js";
import { loadClerkClient } from "/static/portal-clerk.js";

const body = document.body;
const clerkEnabled = body?.dataset.clerkEnabled === "true";
const signOutLinks = document.querySelectorAll("[data-portal-signout]");
const banner = createPortalBanner();
const currentPath = body?.dataset.currentPath || window.location.pathname;
const isAuthSurface = /^\/app\/(login|signup|session\/callback|logout)/.test(currentPath);
const resumeUrl = currentPathWithQuery();
const sessionLossRedirect = `/app/login?reason=invalid_session&resume=${encodeURIComponent(resumeUrl)}`;

let inFlight = false;
let redirectingForSessionLoss = false;

const redirectForSessionLoss = (message) => {
  if (redirectingForSessionLoss || isAuthSurface) {
    return;
  }
  redirectingForSessionLoss = true;
  window.dispatchEvent(
    new CustomEvent("resona:auth-expired", {
      detail: { loginUrl: sessionLossRedirect, reason: "invalid_session" },
    })
  );
  banner.show({
    tone: "warning",
    title: "Session ended",
    message,
  });
  window.setTimeout(() => {
    window.location.assign(sessionLossRedirect);
  }, 700);
};

subscribeSessionEvents((event) => {
  if (!event?.type) {
    return;
  }
  if (event.type === "signed_out") {
    redirectForSessionLoss("This portal session ended in another tab, so we’re reopening sign-in here too.");
  }
  if (event.type === "session_expired") {
    redirectForSessionLoss("Your secure session needs to be refreshed before the portal can continue.");
  }
});

if (!clerkEnabled || signOutLinks.length === 0) {
  // We still keep the cross-tab listener above active on auth pages and stale tabs.
} else {
  signOutLinks.forEach((link) => {
    link.addEventListener("click", async (event) => {
      if (inFlight) {
        event.preventDefault();
        return;
      }
      event.preventDefault();
      inFlight = true;
      const originalText = link.textContent;
      link.textContent = "Signing out...";
      link.setAttribute("aria-disabled", "true");

      try {
        await fetch("/app/auth/clear", {
          method: "POST",
          credentials: "same-origin",
        });
      } catch {
        // Continue through Clerk sign-out even if backend cookie cleanup fails.
      }

      publishSessionEvent("signed_out", { path: resumeUrl });

      try {
        const clerk = await loadClerkClient({
          publishableKey: body?.dataset.clerkPublishableKey || "",
          frontendApiUrl: body?.dataset.clerkFrontendApiUrl || "",
          includeUi: false,
        });
        await Promise.race([
          clerk.signOut({ redirectUrl: "/app/login?logged_out=1" }),
          wait(1600),
        ]);
        window.location.replace("/app/login?logged_out=1");
      } catch (error) {
        console.error("Portal direct sign-out failed", error);
        window.location.assign("/app/logout");
      } finally {
        link.textContent = originalText;
        link.removeAttribute("aria-disabled");
        inFlight = false;
      }
    });
  });
}
