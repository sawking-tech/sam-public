/* Каркас SAM: решение по сообщению приложения из его фрейма (app-client.js -> shell.js).

   Приложение живёт в iframe с CSP sandbox без allow-same-origin, поэтому origin его сообщений - "null", и origin
   сам по себе ничего не доказывает. Принимается только сообщение, чей источник - окно фрейма этого приложения
   (e.source === iframe.contentWindow), с origin "null" (страница в песочнице) или своим origin шлюза. Содержимое
   панели от приложения в DOM каркаса не попадает: это фрейм того же приложения - его путь под /apps/<slug>/ или
   srcdoc, оба с sandbox="allow-scripts".

   Чистая функция: проверяется в node (tests/test_shell.py) без браузера. */
(function (root) {
  const TYPES = ["sam:openPanel", "sam:closePanel"];
  const MAX_TITLE = 80;
  const MAX_HTML = 200000;      // панель - форма или подсказка, не второе приложение

  function safePath(path) {
    // путь внутри приложения: относительный, без схемы, без выхода наверх и без «//»
    if (typeof path !== "string" || !path) return "";
    if (/^[a-z][a-z0-9+.-]*:/i.test(path) || path.startsWith("/") || path.startsWith("\\")) return null;
    if (path.split(/[/\\?#]/).includes("..")) return null;
    return path;
  }

  function decide(event, ctx) {
    // ctx: {source: окно фрейма приложения, origins: принимаемые origin, base: "/apps/<slug>/"}
    if (!event || !ctx || event.source !== ctx.source || !ctx.source) return null;
    if (!(ctx.origins || []).includes(event.origin)) return null;
    const msg = event.data;
    if (!msg || typeof msg !== "object" || !TYPES.includes(msg.type)) return null;
    if (msg.type === "sam:closePanel") return { action: "close" };
    const title = String(msg.title || "").slice(0, MAX_TITLE);
    if (typeof msg.html === "string") {
      if (msg.html.length > MAX_HTML) return null;
      return { action: "open", title, srcdoc: msg.html };
    }
    const path = safePath(msg.path);
    if (path === null) return null;
    return { action: "open", title, src: ctx.base + path };
  }

  const api = { decide, safePath, TYPES };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.SAMBridge = api;
})(typeof window !== "undefined" ? window : globalThis);
