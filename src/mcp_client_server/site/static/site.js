(() => {
  "use strict";

  const COPIED_MS = 1600;

  async function writeClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.append(area);
    area.select();
    const done = document.execCommand("copy");
    area.remove();
    if (!done) throw new Error("copy failed");
  }

  function copySource(button) {
    const install = button.closest(".install");
    const source = install ? install.querySelector("[data-copy-source]") : button.closest(".code").querySelector("pre");
    return source.textContent;
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    const label = button.querySelector(".copy-label");
    try {
      await writeClipboard(copySource(button));
      label.textContent = "Copied";
      button.classList.add("is-copied");
    } catch {
      label.textContent = "Not copied";
    }
    clearTimeout(button.resetTimer);
    button.resetTimer = setTimeout(() => {
      label.textContent = "Copy";
      button.classList.remove("is-copied");
    }, COPIED_MS);
  });

  for (const tabs of document.querySelectorAll("[data-tabs]")) {
    const buttons = [...tabs.querySelectorAll('[role="tab"]')];
    const select = (current, focus) => {
      for (const button of buttons) {
        const selected = button === current;
        button.setAttribute("aria-selected", String(selected));
        button.tabIndex = selected ? 0 : -1;
        document.getElementById(button.getAttribute("aria-controls")).hidden = !selected;
      }
      if (focus) current.focus();
    };
    tabs.addEventListener("click", (event) => {
      const button = event.target.closest('[role="tab"]');
      if (button) select(button, false);
    });
    tabs.addEventListener("keydown", (event) => {
      const index = buttons.indexOf(event.target);
      if (index < 0) return;
      const last = buttons.length - 1;
      const next = { ArrowRight: index === last ? 0 : index + 1, ArrowLeft: index === 0 ? last : index - 1, Home: 0, End: last }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(buttons[next], true);
    });
    select(buttons[0], false);
    tabs.classList.add("is-ready");
  }

  const gallery = document.querySelector("[data-gallery]");
  const controls = document.querySelector("[data-gallery-controls]");
  if (gallery && controls) {
    const previous = controls.querySelector("[data-gallery-previous]");
    const next = controls.querySelector("[data-gallery-next]");
    const step = () => {
      const card = gallery.firstElementChild;
      return card.getBoundingClientRect().width + parseFloat(getComputedStyle(gallery).columnGap || "0");
    };
    const update = () => {
      previous.disabled = gallery.scrollLeft < 8;
      next.disabled = gallery.scrollLeft > gallery.scrollWidth - gallery.clientWidth - 8;
    };
    previous.addEventListener("click", () => gallery.scrollBy({ left: -step() }));
    next.addEventListener("click", () => gallery.scrollBy({ left: step() }));
    gallery.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update, { passive: true });
    controls.hidden = false;
    update();
  }

  // On a small screen the docs pages are one row that scrolls sideways. Bring the current page into view.
  const currentDoc = document.querySelector('.docs-side [aria-current="page"]');
  if (currentDoc) currentDoc.scrollIntoView({ block: "nearest", inline: "center" });

  const toc = document.querySelector("[data-toc]");
  if (toc && "IntersectionObserver" in window) {
    const links = new Map();
    for (const link of toc.querySelectorAll("a")) {
      const heading = document.getElementById(decodeURIComponent(link.hash.slice(1)));
      if (heading) links.set(heading, link);
    }
    const visible = new Set();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.add(entry.target);
          else visible.delete(entry.target);
        }
        const first = [...links.keys()].find((heading) => visible.has(heading));
        if (!first) return;
        for (const [heading, link] of links) link.classList.toggle("is-current", heading === first);
      },
      { rootMargin: "-88px 0px -60% 0px" },
    );
    for (const heading of links.keys()) observer.observe(heading);
  }
})();
