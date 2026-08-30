(() => {
  const storageKey = "wotstat-status-theme";
  const transitionDuration = 620;

  function loadTheme() {
    try {
      const storedTheme = window.localStorage.getItem(storageKey);
      if (storedTheme === "light" || storedTheme === "dark") return storedTheme;
    } catch {
      // Storage may be unavailable in restricted browsing modes.
    }
    return new URL(window.location.href).searchParams.get("theme") === "light"
      ? "light"
      : "dark";
  }

  function removeLegacyThemeParameter() {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("theme")) return;
    url.searchParams.delete("theme");
    window.history.replaceState(window.history.state, "", url);
  }

  function saveTheme(savedTheme) {
    try {
      window.localStorage.setItem(storageKey, savedTheme);
    } catch {
      // Theme switching still works when persistence is unavailable.
    }
  }

  let theme = loadTheme();
  let requestedTheme = theme;

  const root = document.documentElement;
  root.style.setProperty("--theme-transition-duration", `${transitionDuration}ms`);
  const transitionScope = document.querySelector(".theme-transition-scope");
  const themeButton = document.querySelector(".theme-toggle");
  const themeLabel = document.querySelector(".theme-label");
  const themeColor = document.querySelector('meta[name="theme-color"]');
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let transitionSequence = 0;

  root.dataset.themeTransitionApi = transitionScope.startViewTransition
    ? "element-native"
    : "css-fallback";

  function applyThemeButton(buttonTheme) {
    themeButton.dataset.theme = buttonTheme;
    themeLabel.textContent = buttonTheme === "dark" ? "Тёмная" : "Светлая";
    themeButton.setAttribute(
      "aria-label",
      buttonTheme === "dark" ? "Включить светлую тему" : "Включить тёмную тему",
    );
  }

  function applyTheme() {
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    applyThemeButton(theme);
    themeColor.content = theme === "dark" ? "#0b0d10" : "#ffffff";
    saveTheme(theme);
  }

  async function runFallback(nextTheme, originX, originY, radius, sequence) {
    const layer = document.createElement("div");
    const content = document.createElement("div");
    layer.className = "theme-transition-fallback";
    layer.dataset.theme = nextTheme;
    layer.setAttribute("aria-hidden", "true");
    content.className = "theme-transition-fallback__content";
    content.style.transform = `translateY(-${window.scrollY}px)`;

    content.append(transitionScope.cloneNode(true));
    layer.append(content);
    document.body.append(layer);

    layer.style.setProperty("--theme-transition-x", `${originX}px`);
    layer.style.setProperty("--theme-transition-y", `${originY}px`);
    layer.style.setProperty("--theme-transition-radius", `${radius}px`);
    layer.classList.add("theme-transition-fallback--running");

    try {
      await Promise.race([
        new Promise((resolve) => layer.addEventListener("animationend", resolve, { once: true })),
        new Promise((resolve) => window.setTimeout(resolve, transitionDuration + 100)),
      ]);
    } finally {
      if (sequence === transitionSequence) {
        theme = nextTheme;
        applyTheme();
        void root.offsetWidth;
      }
      layer.remove();
    }
  }

  async function toggleTheme() {
    requestedTheme = requestedTheme === "dark" ? "light" : "dark";
    const nextTheme = requestedTheme;
    const sequence = ++transitionSequence;
    applyThemeButton(nextTheme);
    const update = () => {
      if (sequence === transitionSequence) {
        theme = nextTheme;
        applyTheme();
      }
    };

    if (reducedMotion.matches) {
      update();
      return;
    }

    const buttonRect = themeButton.getBoundingClientRect();
    const originX = buttonRect.left + buttonRect.width / 2;
    const originY = buttonRect.top + buttonRect.height / 2;
    const fallbackRadius = Math.hypot(
      Math.max(originX, window.innerWidth - originX),
      Math.max(originY, window.innerHeight - originY),
    );
    root.dataset.themeTransition = "running";

    try {
      if (transitionScope.startViewTransition) {
        const transition = transitionScope.startViewTransition(update);
        await transition.ready;

        const scopeRect = transitionScope.getBoundingClientRect();
        const localX = originX - scopeRect.left;
        const localY = originY - scopeRect.top;
        const scopeCorners = [
          [0, 0],
          [scopeRect.width, 0],
          [0, scopeRect.height],
          [scopeRect.width, scopeRect.height],
        ];
        const radius = Math.max(
          ...scopeCorners.map(([x, y]) => Math.hypot(localX - x, localY - y)),
        );
        const radiusBasis = Math.hypot(scopeRect.width, scopeRect.height) / Math.SQRT2;
        const centerX = (localX / scopeRect.width) * 100;
        const centerY = (localY / scopeRect.height) * 100;
        const center = `${centerX}% ${centerY}%`;
        const radiusPercent = (radius / radiusBasis) * 100;

        transitionScope.animate(
          {
            clipPath: [`circle(0 at ${center})`, `circle(${radiusPercent}% at ${center})`],
          },
          {
            duration: transitionDuration,
            easing: "cubic-bezier(0.22, 1, 0.36, 1)",
            pseudoElement: "::view-transition-new(root)",
          },
        );
        await transition.finished;
      } else {
        await runFallback(nextTheme, originX, originY, fallbackRadius, sequence);
      }
    } catch {
      // Starting a newer transition may skip the previous one.
      if (sequence === transitionSequence && theme !== nextTheme) {
        theme = nextTheme;
        applyTheme();
      }
    } finally {
      if (sequence === transitionSequence) delete root.dataset.themeTransition;
    }
  }

  removeLegacyThemeParameter();
  themeButton.addEventListener("click", toggleTheme);
  applyTheme();
})();
