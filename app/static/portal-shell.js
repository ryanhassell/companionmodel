(function () {
  const body = document.body;
  const clerkEnabled = body?.dataset.clerkEnabled === "true";
  const signOutLinks = document.querySelectorAll("[data-portal-signout]");

  if (!clerkEnabled || signOutLinks.length === 0) {
    return;
  }

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

  const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  let inFlight = false;

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

      try {
        const clerk = await waitForClerk();
        if (!clerk.loaded) {
          await clerk.load({
            publishableKey: body?.dataset.clerkPublishableKey || "",
            ...(body?.dataset.clerkFrontendApiUrl
              ? { frontendApi: body.dataset.clerkFrontendApiUrl }
              : {}),
          });
        }
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
})();
