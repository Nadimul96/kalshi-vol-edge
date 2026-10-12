// kalshi-vol-edge: theme toggle, chart hover, live countdown and fragment polling.
// No dependencies; every page renders fully without this file.
(() => {
  "use strict";
  const root = document.documentElement;
  const tip = document.querySelector(".tooltip");

  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-theme-toggle]")) return;
    const next = root.dataset.theme === "light" ? "dark" : "light";
    root.dataset.theme = next;
    try {
      localStorage.setItem("kve-theme", next);
    } catch {
      /* storage can be unavailable (private mode); the toggle still works for this page */
    }
  });

  function showTip(text, x, y) {
    if (!tip) return;
    tip.textContent = text;
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    let left = x + 14;
    let top = y + 14;
    if (left + box.width > window.innerWidth - 8) left = x - box.width - 14;
    if (top + box.height > window.innerHeight - 8) top = y - box.height - 14;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }

  function hideTip() {
    if (tip) tip.hidden = true;
  }

  document.addEventListener("pointermove", (event) => {
    const target = event.target.closest("[data-tip]");
    if (target) showTip(target.dataset.tip, event.clientX, event.clientY);
  });
  document.addEventListener("pointerout", (event) => {
    if (event.target.closest("[data-tip]")) hideTip();
  });

  function bindChart(figure) {
    if (figure.dataset.bound) return;
    figure.dataset.bound = "1";
    const points = JSON.parse(figure.dataset.points || "[]");
    const plot = figure.querySelector(".chart-plot");
    if (!points.length || !plot) return;
    const cross = document.createElement("span");
    const dot = document.createElement("span");
    cross.className = "crosshair";
    dot.className = "hover-dot";
    cross.hidden = dot.hidden = true;
    plot.append(cross, dot);
    plot.addEventListener("pointermove", (event) => {
      const box = plot.getBoundingClientRect();
      const x = ((event.clientX - box.left) / box.width) * 100;
      let best = points[0];
      for (const p of points) if (Math.abs(p[0] - x) < Math.abs(best[0] - x)) best = p;
      cross.style.left = `${best[0]}%`;
      dot.style.left = `${best[0]}%`;
      dot.style.top = `${best[1]}%`;
      cross.hidden = dot.hidden = false;
      showTip(best[2], event.clientX, event.clientY);
    });
    plot.addEventListener("pointerleave", () => {
      cross.hidden = dot.hidden = true;
      hideTip();
    });
  }

  function bindCharts(scope) {
    scope.querySelectorAll(".chart[data-points]").forEach(bindChart);
  }

  function tick() {
    const now = Date.now() / 1000;
    document.querySelectorAll("[data-countdown]").forEach((el) => {
      const s = Math.max(0, Math.round(Number(el.dataset.countdown) - now));
      el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    });
    document.querySelectorAll("[data-progress]").forEach((el) => {
      const open = Number(el.dataset.open);
      const close = Number(el.dataset.close);
      const pct = ((now - open) / (close - open)) * 100;
      el.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    });
  }

  function startPolling(box) {
    const url = box.dataset.poll;
    const every = Number(box.dataset.interval) || 5000;
    const dot = document.querySelector("[data-refresh-dot]");
    const label = document.querySelector("[data-refresh-label]");
    let failures = 0;

    const setState = (ok) => {
      if (dot) dot.className = `dot ${ok ? "dot-good" : "dot-bad"}`;
      if (label) {
        label.textContent = ok
          ? `Auto-refresh ${every / 1000}s`
          : "Dashboard server unreachable, retrying";
      }
    };
    const schedule = () => {
      const wait = failures ? Math.min(every * 2 ** failures, 60000) : every;
      window.setTimeout(poll, wait);
    };
    async function poll() {
      if (document.hidden) return schedule();
      try {
        const res = await fetch(url, { headers: { Accept: "text/html" }, cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        box.innerHTML = await res.text();
        bindCharts(box);
        tick();
        failures = 0;
        setState(true);
      } catch {
        failures += 1;
        setState(false);
      }
      schedule();
    }
    schedule();
  }

  bindCharts(document);
  tick();
  window.setInterval(tick, 1000);
  document.querySelectorAll("[data-poll]").forEach(startPolling);
})();
