/* Каркас страниц SAM: полоса с навигацией, рамка и выдвижная панель (механика - panel.js, копия из
   DotNetSolutionKit) и API панелей для страниц.

   Подключение (страница SAM; G - адрес шлюза, у страниц шлюза пусто):
     <link rel="stylesheet" href="G/static/shell/shell.css"><link rel="stylesheet" href="G/static/shell/sam.css">
     <script src="G/shell/config.js"></script><script src="G/static/shell/boot.js"></script>
     <script src="G/static/shell/shell.js" defer data-page="apps"></script>
   Переопределение каркаса - data-атрибуты этого тега: data-shell="off" (своя полноэкранная страница),
   data-bar="off" (без полосы), data-panelw="360", data-maxw="4000" (ширина колонки; больше окна - панель всегда
   выдвижная), data-fill="1" (контент занимает окно под полосой, прокрутка внутри страницы).

   Полоса (docs/features/shell.md): слева знак (домашний экран приложений) и навигация, по центру статус
   страницы, справа индикаторы страницы, тема и кнопка панели.

   API (window.SAMShell):
     openPanel({title, node | src | srcdoc | tabs, tab, side, onClose}) - панель с содержимым, одна за раз; tabs -
       [{key, title, node}] - вкладки; tab без содержимого - открыть панель по умолчанию на этой вкладке; side -
       "left" или "right";
     closePanel() - закрыть, фокус вернуть;
     setDefaultPanel(title, node | tabs) - что панель показывает без вызова (по умолчанию - страницы SAM);
     setStatus(node | text) - статус страницы в центре полосы; setTools(...nodes) - индикаторы и кнопки справа;
     setBusy(bool) - «SAM думает»: скан-луч под полосой; setVoice(bool) - эквалайзер голоса у статуса;
     connectApp(iframe, slug) - сообщения приложения из этого фрейма (bridge.js). */
(function () {
  const script = document.currentScript;
  const opt = Object.assign({}, script.dataset);
  const cfg = window.SAM_SHELL || { nav: [] };
  const gateway = new URL(script.src, location.href).origin;
  const root = document.documentElement;
  const noop = () => {};
  // знак SAM иконкой вкладки на любой странице каркаса, с какого бы порта она ни шла: без него браузер просит
  // /favicon.ico у своего хоста и получает 404
  if (!document.querySelector('link[rel~="icon"]')) {
    const icon = Object.assign(document.createElement("link"), { rel: "icon", type: "image/svg+xml",
                                                                 href: `${gateway}/static/shell/mark.svg` });
    document.head.append(icon);
  }

  if (opt.shell === "off") {
    root.classList.remove("unlaid");
    window.SAMShell = { enabled: false, openPanel: noop, closePanel: noop, setDefaultPanel: noop, connectApp: noop,
                        setStatus: noop, setTools: noop, setBusy: noop, setVoice: noop };
    return;
  }
  root.classList.add("sam-shell");          // страница в каркасе: её фиксированные элементы встают под полосу
  // до panel.js: он читает --panelw и --maxw из CSS один раз при загрузке
  if (opt.panelw) root.style.setProperty("--panelw", parseInt(opt.panelw, 10) + "px");
  if (opt.maxw) root.style.setProperty("--maxw", parseInt(opt.maxw, 10) + "px");
  if (opt.bar === "off") root.classList.add("sam-nobar");
  if (opt.fill === "1") root.classList.add("sam-fill");

  function el(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      node.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids) node.append(kid);
    return node;
  }

  function navUrl(item) {
    const port = item.port ? ":" + item.port : "";
    return `${location.protocol}//${location.hostname}${port}${item.path || "/"}`;
  }

  function navLinks(cls) {
    return (cfg.nav || []).map((item) => el("a", {
      href: navUrl(item), class: cls, "aria-current": item.key === opt.page ? "page" : null,
      title: item.pinned ? "закреплено" : null,
    }, item.name));
  }

  // ── рамка: разметка каркаса DotNetSolutionKit (index.html), контент страницы - внутри #below ──
  const content = el("div", { id: "sam-content" });
  while (document.body.firstChild) content.append(document.body.firstChild);
  const sideBody = el("div", { class: "side-body", id: "side-body" });
  const sideTitle = el("span", { class: "side-title" });
  const back = el("button", { id: "side-back", type: "button", title: "Закрыть", "aria-label": "Закрыть панель",
                              hidden: true }, "✕");
  const sidebar = el("aside", { id: "sidebar", "aria-label": "Панель" },
    el("div", { class: "side-head" }, sideTitle,
      el("button", { id: "side-lang", type: "button", hidden: true }, "RU"),
      el("button", { id: "side-theme", type: "button", title: "Тема", "aria-label": "Тема" }, "◐"),
      el("button", { id: "side-swap", type: "button", title: "Перенести панель на другую сторону",
                     "aria-label": "Перенести панель на другую сторону" }, "⇄"),
      back,
      el("button", { id: "side-close", type: "button", title: "Закрыть", "aria-label": "Закрыть панель" }, "✕")),
    sideBody);
  const status = el("span", { class: "spacer sam-status", id: "sam-status", role: "status" });
  const eq = el("span", { class: "sam-eq", "aria-label": "голос", role: "img" }, el("i"), el("i"), el("i"), el("i"));
  const tools = el("span", { class: "sam-tools", id: "sam-tools" });
  const themeBtn = el("button", { id: "bar-theme", type: "button", title: "Тема", "aria-label": "Тема" }, "◐");
  const home = (cfg.nav || []).find((i) => i.key === "apps") || { path: "/" };
  const bar = el("div", { class: "bar" }, el("div", { class: "wrap bar-inner" },
    el("button", { id: "burger", type: "button", "aria-expanded": "false", "aria-controls": "sidebar",
                   title: "Панель" }, "≡"),
    el("a", { class: "bar__mark", href: navUrl(home), title: "Приложения SAM" },
      el("span", { class: "sam-mark", "aria-hidden": "true" }, "◆"), "SAM"),
    el("nav", { class: "sam-nav-bar", "aria-label": "Страницы SAM" }, ...navLinks("")),
    eq, status, tools, themeBtn));
  document.body.append(el("div", { id: "main" }, el("div", { id: "cols" }, sidebar,
    el("div", { id: "below" }, bar, content))));

  // ── панель: своё содержимое поверх содержимого по умолчанию ──
  const fallback = { title: "SAM", node: el("ul", { class: "sam-nav" },
    ...navLinks("").map((a) => el("li", {}, a))) };
  let byDefault = fallback, current = null;

  function show(title, node) {
    sideTitle.textContent = title || "";
    sideBody.replaceChildren(node);
  }

  function tabbed(tabs, active) {
    // вкладки панели: полоса кнопок и одна видимая вкладка; заголовок вкладки может быть узлом (счётчик)
    const line = el("span", { class: "sam-tab-line", "aria-hidden": "true" });
    const strip = el("div", { class: "sam-tabs", role: "tablist" }, line);
    const body = el("div", { class: "sam-tab-body", role: "tabpanel" });
    const box = el("div", { class: "sam-tabbed" }, strip, body);
    const buttons = new Map();
    for (const t of tabs) {
      const b = el("button", { type: "button", role: "tab", class: "sam-tab", "data-tab": t.key }, t.title);
      b.addEventListener("click", () => box.select(t.key));
      buttons.set(t.key, b);
      strip.append(b);
    }
    box.select = (key) => {
      const t = tabs.find((x) => x.key === key) || tabs[0];
      for (const [k, b] of buttons) b.setAttribute("aria-selected", String(k === t.key));
      body.replaceChildren(t.node);
      box.dataset.active = t.key;
      // подчёркивание переезжает к выбранной вкладке: только transform, после раскладки
      requestAnimationFrame(() => {
        const b = buttons.get(t.key);
        line.style.transform = `translateX(${b.offsetLeft}px) scaleX(${b.offsetWidth})`;
      });
    };
    box.select(active);
    return box;
  }

  function setSide(side) {
    // сторона панели - та же механика, что у кнопки ⇄ panel.js (st.side, applySide)
    if (!side || typeof st === "undefined" || st.side === side) return;
    st.side = side;
    applySide();
  }

  function frame(src, srcdoc, title) {
    // содержимое от приложения - только в своём фрейме, с той же песочницей, что у страницы приложения
    const f = el("iframe", { class: "sam-panel-frame", sandbox: "allow-scripts", title: title || "панель" });
    if (srcdoc !== undefined) f.srcdoc = srcdoc; else f.src = src;
    return f;
  }

  function openPanel(o) {
    const opts = o || {};
    setSide(opts.side);
    if (opts.tab && !opts.node && !opts.tabs && !opts.src && opts.srcdoc === undefined && byDefault.node.select) {
      if (current) closePanel(true);                    // вкладка панели по умолчанию
      byDefault.node.select(opts.tab);
      show(byDefault.title, byDefault.node);
      if (typeof setNav === "function" && !document.body.classList.contains("wide")) setNav(true);
      return;
    }
    if (current) closePanel(true);
    const node = opts.tabs ? tabbed(opts.tabs, opts.tab)
      : opts.node || frame(opts.src, opts.srcdoc, opts.title);
    current = { onClose: opts.onClose, focus: document.activeElement,
                wasOpen: document.body.classList.contains("nav-open") };
    show(opts.title, node);
    back.hidden = false;
    if (typeof setNav === "function" && !document.body.classList.contains("wide")) setNav(true);
    const first = sideBody.querySelector("button, a, input, select, textarea, iframe, [tabindex]");
    (first || back).focus();
  }

  function closePanel(replacing) {
    if (!current) return;
    const was = current;
    current = null;
    show(byDefault.title, byDefault.node);
    back.hidden = true;
    if (!replacing && !was.wasOpen && typeof setNav === "function") setNav(false);
    if (was.onClose) was.onClose();
    if (!replacing && was.focus && was.focus.focus) was.focus.focus();
  }

  function setDefaultPanel(title, node) {
    byDefault = !node ? fallback : { title, node: Array.isArray(node) ? tabbed(node) : node };
    if (!current) show(byDefault.title, byDefault.node);
  }

  function setStatus(what) { status.replaceChildren(what || ""); }
  function setBusy(on) { root.classList.toggle("sam-busy", !!on); }
  function setVoice(on) { root.classList.toggle("sam-voice", !!on); }
  function setTools(...nodes) { tools.replaceChildren(...nodes); }

  function connectApp(iframe, slug) {
    const ctx = () => ({ source: iframe.contentWindow, origins: ["null", gateway], base: `/apps/${slug}/` });
    addEventListener("message", (e) => {
      const d = window.SAMBridge && SAMBridge.decide(e, ctx());
      if (!d) return;
      if (d.action === "close") closePanel();
      else openPanel({ title: d.title, src: d.src, srcdoc: d.srcdoc });
    });
  }

  back.addEventListener("click", () => closePanel());
  // тема из полосы - та же смена, что у кнопки панели (panel.js cycleTheme); лицо кнопки - как у неё
  // смена темы мгновенная во всех зонах: переходы выключены, пока новый стиль не отрисован (два кадра)
  function instantTheme(doc = document) {
    const r = doc.documentElement;
    r.classList.add("sam-theme-switch");
    requestAnimationFrame(() => requestAnimationFrame(() => r.classList.remove("sam-theme-switch")));
  }
  // фреймы своего origin (панель, приложение) получают тему в том же кадре; чужой origin недоступен - пропуск
  function themeFrames() {
    const theme = root.getAttribute("data-theme");
    for (const f of document.querySelectorAll("iframe")) {
      let doc;
      try { doc = f.contentDocument; } catch { continue; }
      if (!doc || !doc.documentElement) continue;
      instantTheme(doc);
      doc.documentElement.setAttribute("data-theme", theme);
    }
  }
  new MutationObserver(themeFrames).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  document.getElementById("side-theme").addEventListener("click", () => instantTheme(), true);
  themeBtn.addEventListener("click", () => {
    instantTheme();
    if (typeof cycleTheme === "function") cycleTheme();
    themeBtn.textContent = document.getElementById("side-theme").textContent;
    themeBtn.title = document.getElementById("side-theme").title;
  });
  document.getElementById("side-close").addEventListener("click", () => closePanel());
  addEventListener("keydown", (e) => { if (e.key === "Escape" && current) closePanel(); });
  show(byDefault.title, byDefault.node);

  window.SAMShell = { enabled: true, gateway, openPanel, closePanel: () => closePanel(), setDefaultPanel,
                      setStatus, setTools, setBusy, setVoice, connectApp, navUrl };

  // механика рамки и панели - panel.js как есть; он ищет элементы рамки сразу при загрузке
  // переходы возвращаются через два кадра после panel.js, как в index.html DotNetSolutionKit: панель и лист
  // уже стоят там, где их оставили, и открытие не проигрывается заново при каждой загрузке
  root.classList.add("preload");
  for (const name of ["bridge.js", "panel.js"]) {
    const s = el("script", { src: `${gateway}/static/shell/${name}` });
    s.async = false;
    if (name === "panel.js") {
      s.onload = () => {
        themeBtn.textContent = document.getElementById("side-theme").textContent;
        requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("preload")));
      };
    }
    document.body.append(s);
  }
})();
