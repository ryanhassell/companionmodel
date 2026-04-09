import "https://esm.sh/@clerk/ui@1.5.0/register";

const CLERK_UI_SRC = "https://cdn.jsdelivr.net/npm/@clerk/ui@1.5.0/dist/ui.shared.browser.js";

export const waitForClerk = ({ timeoutMs = 6000 } = {}) =>
  new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      if (window.Clerk) {
        window.clearInterval(timer);
        resolve(window.Clerk);
        return;
      }
      if (Date.now() - startedAt >= timeoutMs) {
        window.clearInterval(timer);
        reject(new Error("Clerk script did not load"));
      }
    }, 50);
  });

export const loadSharedUiBundle = () =>
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
    script.src = CLERK_UI_SRC;
    script.onload = () => resolve(window.__internal_ClerkUICtor);
    script.onerror = () => reject(new Error("Clerk UI bundle failed to load"));
    document.head.appendChild(script);
  });

export const loadClerkClient = async ({ publishableKey, frontendApiUrl, includeUi = false } = {}) => {
  if (includeUi) {
    await loadSharedUiBundle();
  }
  const clerk = await waitForClerk();
  if (!clerk.loaded) {
    await clerk.load({
      publishableKey: publishableKey || "",
      ...(frontendApiUrl ? { frontendApi: frontendApiUrl } : {}),
      ...(includeUi ? { clerkUICtor: window.__internal_ClerkUICtor } : {}),
    });
  }
  return clerk;
};

export const clerkPrimaryEmail = (clerk = window.Clerk) =>
  clerk?.user?.primaryEmailAddress?.emailAddress ||
  clerk?.user?.emailAddresses?.[0]?.emailAddress ||
  "";

export const clerkDisplayName = (clerk = window.Clerk) =>
  clerk?.user?.fullName ||
  [clerk?.user?.firstName, clerk?.user?.lastName].filter(Boolean).join(" ").trim() ||
  clerk?.user?.username ||
  "";
