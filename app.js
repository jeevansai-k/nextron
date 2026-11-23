// NEXTRON website interactions. No dependencies, no tracking.
(() => {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- sticky nav border ----
  const nav = document.querySelector(".nav");
  const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // ---- hero status line: EXPOSED -> CONNECTING -> PROTECTED ----
  const state = document.getElementById("sb-state");
  const ip = document.getElementById("sb-ip");
  const chain = document.getElementById("sb-chain");
  chain.innerHTML =
    'you <span class="arrow">⟶</span> <span class="hop">VPN</span> <span class="arrow">⟶</span> ' +
    '<span class="hop">Tor</span> <span class="arrow">⟶</span> internet';
  const spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  const phases = [
    { s: "exposed", label: "◌ EXPOSED", ip: "direct connection", ms: 2200 },
    { s: "connecting", label: "CONNECTING", ip: "Tor bootstrap 68%", ms: 2600 },
    { s: "protected", label: "◉ PROTECTED", ip: "198.51.100.71 · NL", ms: 5200 },
  ];
  let phase = 0, frame = 0, spin = null;
  const show = () => {
    const p = phases[phase];
    state.dataset.state = p.s;
    ip.textContent = p.ip;
    chain.classList.toggle("live", p.s === "protected");
    clearInterval(spin);
    if (p.s === "connecting" && !reduced) {
      spin = setInterval(() => {
        state.textContent = `${spinner[frame++ % spinner.length]} ${p.label}`;
      }, 90);
    } else {
      state.textContent = p.s === "connecting" ? `⠹ ${p.label}` : p.label;
    }
    if (!reduced) setTimeout(() => { phase = (phase + 1) % phases.length; show(); }, p.ms);
  };
  if (reduced) phase = 2;
  show();

  // ---- routes explorer ----
  const ROUTES = {
    tor: {
      title: "Tor only",
      hops: [["you"], ["Tor", "3 relays", "n-tor"], ["internet", "", "n-net"]],
      pitch: "Every connection leaves through the Tor network, with the exit identity rotated on your schedule.",
      sees: "Your ISP sees Tor. The exit relay sees the destination, never you.",
      needs: "Tor installed. Root for system-wide routing; without it, apps use the SOCKS proxy.",
    },
    vpn: {
      title: "VPN only",
      hops: [["you"], ["VPN", "OpenVPN / WG", "n-vpn"], ["internet", "", "n-net"]],
      pitch: "One encrypted tunnel carries everything, with a stable, fast exit and optional shuffling across your profiles.",
      sees: "Your ISP sees a VPN. The provider you chose sees the rest.",
      needs: "An OpenVPN or WireGuard profile, and root to create the tunnel.",
    },
    torvpn: {
      title: "Tor over VPN",
      hops: [["you"], ["VPN", "first hop", "n-vpn"], ["Tor", "3 relays", "n-tor"], ["internet", "", "n-net"]],
      pitch: "Tor runs inside the VPN, so every relay connection it opens is already encrypted before it leaves your machine.",
      sees: "Your ISP sees a VPN, not Tor. Tor's guard sees the VPN, not you.",
      needs: "A VPN profile, Tor, and root.",
    },
    vpntor: {
      title: "VPN over Tor",
      hops: [["you"], ["Tor", "3 relays", "n-tor"], ["VPN", "exit", "n-vpn"], ["internet", "", "n-net"]],
      pitch: "Your VPN connection travels through Tor, so the provider never learns your real address, and sites see a stable VPN exit.",
      sees: "The VPN provider sees a Tor exit. Sites see the VPN.",
      needs: "A TCP OpenVPN profile (Tor can't carry UDP), Tor, and root.",
    },
  };
  const chainEl = document.getElementById("chain");
  const set = (id, text) => { document.getElementById(id).textContent = text; };
  const renderRoute = (key) => {
    const r = ROUTES[key];
    chainEl.innerHTML = "";
    r.hops.forEach(([name, note, cls], i) => {
      if (i) {
        const link = document.createElement("span");
        link.className = "link";
        link.style.animationDelay = `${i * 120 - 60}ms`;
        chainEl.appendChild(link);
      }
      const node = document.createElement("span");
      node.className = `node ${cls || ""}`;
      node.style.animationDelay = `${i * 120}ms`;
      node.innerHTML = `${name}${note ? `<small>${note}</small>` : ""}`;
      chainEl.appendChild(node);
    });
    set("route-title", r.title);
    set("route-pitch", r.pitch);
    set("route-sees", r.sees);
    set("route-needs", r.needs);
  };
  const tabs = [...document.querySelectorAll(".route-tabs button")];
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
      renderRoute(tab.dataset.route);
    });
    tab.addEventListener("keydown", (e) => {
      const i = tabs.indexOf(tab);
      const next = e.key === "ArrowRight" ? tabs[(i + 1) % tabs.length]
        : e.key === "ArrowLeft" ? tabs[(i - 1 + tabs.length) % tabs.length] : null;
      if (next) { next.focus(); next.click(); }
    });
  });
  renderRoute("tor");

  // ---- verification checklist ----
  const checks = [...document.querySelectorAll("#checks li")];
  const runChecks = () => {
    checks.forEach((li) => li.classList.remove("done"));
    if (reduced) { checks.forEach((li) => li.classList.add("done")); return; }
    checks.forEach((li, i) => setTimeout(() => li.classList.add("done"), 260 + i * 320));
  };
  document.getElementById("replay").addEventListener("click", runChecks);
  let checked = false;
  new IntersectionObserver((entries, obs) => {
    if (entries.some((e) => e.isIntersecting) && !checked) { checked = true; runChecks(); obs.disconnect(); }
  }, { threshold: 0.35 }).observe(document.querySelector(".checklist"));

  // ---- copy buttons ----
  document.querySelectorAll(".copy").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.parentElement.querySelector("code").textContent;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "Copied";
        btn.classList.add("copied");
      } catch {
        btn.textContent = "Select";
      }
      setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 1600);
    });
  });

  // ---- spotlight on cards ----
  document.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("pointermove", (e) => {
      const r = card.getBoundingClientRect();
      card.style.setProperty("--mx", `${e.clientX - r.left}px`);
      card.style.setProperty("--my", `${e.clientY - r.top}px`);
    });
  });

  // ---- staggered reveal ----
  const targets = document.querySelectorAll(".section-head, .routes, .split > *, .state, .card, .install-steps li, .keys, .plan, .plan-note, .faq details, .band-inner, .final");
  if (!reduced && "IntersectionObserver" in window) {
    targets.forEach((el, i) => { el.classList.add("reveal"); el.style.transitionDelay = `${(i % 4) * 70}ms`; });
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
    }, { threshold: 0.12 });
    targets.forEach((el) => io.observe(el));
  }
})();
