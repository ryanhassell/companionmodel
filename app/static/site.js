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

  window.addEventListener("beforeunload", () => {
    teardown.forEach((fn) => fn());
  });
})();
