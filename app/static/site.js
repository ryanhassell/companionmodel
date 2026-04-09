(function () {
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const teardown = [];

  const parseVariants = (node) => {
    try {
      const variants = JSON.parse(node.getAttribute("data-typed-variants") || "[]");
      if (Array.isArray(variants) && variants.length > 0) {
        return variants;
      }
    } catch {
      // fall through to text content
    }
    return [node.textContent || ""];
  };

  const typeInto = (node, text, speedMs) =>
    new Promise((resolve) => {
      node.textContent = "";
      node.classList.add("typed-ready");
      node.classList.add("is-typing");
      let index = 0;
      const timer = window.setInterval(() => {
        index += 1;
        node.textContent = text.slice(0, index);
        if (index >= text.length) {
          window.clearInterval(timer);
          node.classList.remove("is-typing");
          resolve();
        }
      }, speedMs);
      teardown.push(() => window.clearInterval(timer));
    });

  const animatedNodes = document.querySelectorAll("[data-animate]");
  if (!reduceMotion && animatedNodes.length > 0) {
    const observer = new IntersectionObserver(
      (entries, obs) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const siblings = Array.from(entry.target.parentElement?.querySelectorAll(":scope > [data-animate]") || []);
            const siblingIndex = Math.max(siblings.indexOf(entry.target), 0);
            entry.target.style.transitionDelay = `${Math.min(siblingIndex * 90, 360)}ms`;
            entry.target.classList.add("in-view");
            obs.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.14 }
    );
    animatedNodes.forEach((node) => observer.observe(node));
  } else {
    animatedNodes.forEach((node) => node.classList.add("in-view"));
  }

  const consoles = document.querySelectorAll(".resona-console");
  consoles.forEach((consoleNode) => {
    const sequenceNodes = [
      ...consoleNode.querySelectorAll(".console-lines .line p"),
      ...consoleNode.querySelectorAll(".console-foot .meta-value"),
    ];
    if (sequenceNodes.length === 0) {
      return;
    }
    const sequenceVariants = sequenceNodes.map(parseVariants);
    const sequenceContainers = sequenceNodes.map((node) =>
      node.closest(".line") || node.closest(".console-foot > div")
    );

    if (reduceMotion) {
      sequenceNodes.forEach((node, i) => {
        node.textContent = sequenceVariants[i][0];
        node.classList.add("typed-ready");
        sequenceContainers[i]?.classList.add("seq-visible");
      });
      return;
    }

    sequenceNodes.forEach((node) => {
      node.textContent = "";
    });
    sequenceContainers.forEach((container) => {
      container?.classList.remove("seq-visible");
    });

    (async () => {
      for (let i = 0; i < sequenceNodes.length; i += 1) {
        const variants = sequenceVariants[i];
        const text = variants[0];
        sequenceContainers[i]?.classList.add("seq-visible");
        await typeInto(sequenceNodes[i], text, 18);
        if (i < sequenceNodes.length - 1) {
          await new Promise((resolve) => {
            const waitTimer = window.setTimeout(resolve, i < 2 ? 550 : 350);
            teardown.push(() => window.clearTimeout(waitTimer));
          });
        }
      }
    })();
  });

  const typedNodes = Array.from(document.querySelectorAll("[data-typed-variants]")).filter(
    (node) => !node.closest(".resona-console")
  );

  const startTypedNode = (node, nodeIndex) => {
    if (node.dataset.typedStarted === "1") {
      return;
    }
    node.dataset.typedStarted = "1";
    const variants = parseVariants(node);

    if (reduceMotion) {
      node.textContent = variants[0];
      node.classList.add("typed-ready");
      return;
    }

    node.textContent = "";
    const startDelayTimer = window.setTimeout(() => {
      typeInto(node, variants[0], 18);
    }, 220 + nodeIndex * 130);
    teardown.push(() => window.clearTimeout(startDelayTimer));
  };

  if (typedNodes.length > 0) {
    if (reduceMotion) {
      typedNodes.forEach((node, index) => startTypedNode(node, index));
    } else {
      const typedObserver = new IntersectionObserver(
        (entries, obs) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              const node = entry.target;
              const idx = typedNodes.indexOf(node);
              startTypedNode(node, idx < 0 ? 0 : idx);
              obs.unobserve(node);
            }
          });
        },
        { threshold: 0.18 }
      );
      typedNodes.forEach((node) => typedObserver.observe(node));
      teardown.push(() => typedObserver.disconnect());
    }
  }

  const body = document.body;
  const clerkEnabled = body?.dataset.clerkEnabled === "true";
  const authSignedOutNodes = document.querySelectorAll("[data-auth-signed-out]");
  const authSignedInNodes = document.querySelectorAll("[data-auth-signed-in]");
  const avatarNodes = document.querySelectorAll("[data-user-avatar]");
  const nameNodes = document.querySelectorAll("[data-user-name]");
  const authLinkNodes = document.querySelectorAll("[data-auth-link]");
  const signOutButtons = document.querySelectorAll("[data-auth-signout]");

  const showSignedOutState = () => {
    authSignedOutNodes.forEach((node) => {
      node.hidden = false;
    });
    authSignedInNodes.forEach((node) => {
      node.hidden = true;
    });
  };

  const showSignedInState = (user) => {
    const primaryEmail = user?.primaryEmailAddress?.emailAddress || "";
    const safeEmail = /@clerk\.local$/i.test(primaryEmail) ? "" : primaryEmail;
    const safeUsername = /^user_[a-z0-9]+$/i.test(user?.username || "") ? "" : user?.username || "";
    const displayName =
      user?.fullName ||
      user?.firstName ||
      safeUsername ||
      safeEmail ||
      "My account";
    const avatarUrl = user?.imageUrl || "/static/images/resona-mark.svg";

    authSignedOutNodes.forEach((node) => {
      node.hidden = true;
    });
    authSignedInNodes.forEach((node) => {
      node.hidden = false;
    });
    avatarNodes.forEach((node) => {
      node.setAttribute("src", avatarUrl);
    });
    nameNodes.forEach((node) => {
      node.textContent = displayName;
    });
    authLinkNodes.forEach((node) => {
      const key = node.getAttribute("data-auth-link");
      const target =
        key === "billing"
          ? "/app/billing"
          : key === "security"
            ? "/app/security"
            : key === "portal"
              ? "/app/dashboard"
              : "/app/landing";
      node.setAttribute("href", `/app/session/callback?next=${encodeURIComponent(target)}`);
    });
  };

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
      teardown.push(() => window.clearInterval(timer));
    });

  const clerkPrimaryEmail = (clerk) =>
    clerk?.user?.primaryEmailAddress?.emailAddress ||
    clerk?.user?.emailAddresses?.[0]?.emailAddress ||
    "";

  const clerkDisplayName = (clerk) =>
    clerk?.user?.fullName ||
    [clerk?.user?.firstName, clerk?.user?.lastName].filter(Boolean).join(" ").trim() ||
    clerk?.user?.username ||
    "";

  const syncClerkSession = async (clerk) => {
    if (!clerk?.session) {
      return false;
    }
    const token = await clerk.session.getToken();
    if (!token) {
      return false;
    }
    const response = await fetch("/app/auth/sync", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        email: clerkPrimaryEmail(clerk),
        display_name: clerkDisplayName(clerk),
      }),
    });
    return response.ok;
  };

  const clearClerkSession = async () => {
    try {
      await fetch("/app/auth/clear", {
        method: "POST",
        credentials: "same-origin",
      });
    } catch {
      // Ignore cleanup failures; Clerk sign-out will still run.
    }
  };

  if (clerkEnabled) {
    (async () => {
      try {
        const clerk = await waitForClerk();
        if (!clerk.loaded) {
          const publishableKey = body?.dataset.clerkPublishableKey || "";
          const frontendApi = body?.dataset.clerkFrontendApiUrl || "";
          await clerk.load({
            publishableKey,
            ...(frontendApi ? { frontendApi } : {}),
          });
        }
        if (clerk.user && clerk.session) {
          await syncClerkSession(clerk);
          showSignedInState(clerk.user);
          signOutButtons.forEach((button) => {
            button.addEventListener("click", async () => {
              await clearClerkSession();
              await clerk.signOut({ redirectUrl: "/" });
            });
          });
          return;
        }
      } catch (error) {
        console.error("Marketing Clerk init failed", error);
      }
    })();
  }

  if (!clerkEnabled) {
    showSignedOutState();
  }

  window.addEventListener("beforeunload", () => {
    teardown.forEach((fn) => fn());
  });
})();
