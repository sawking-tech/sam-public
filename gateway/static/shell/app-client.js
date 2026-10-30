/* Клиент каркаса SAM для страницы приложения: панели каркаса без своего кода панелей.

   <script src="/static/shell/app-client.js"></script>
   SAMApp.openPanel({title: "Настройки", path: "settings.html"})   // страница приложения в панели
   SAMApp.openPanel({title: "Подсказка", html: "<p>...</p>"})        // разметка в панели (в своём фрейме)
   SAMApp.closePanel()

   Каркас рисует панель своей механикой и кладёт содержимое во фрейм приложения: доступа к странице каркаса у
   приложения нет. Вне каркаса (страница открыта отдельно) вызовы ничего не делают. */
(function () {
  const inShell = window.parent && window.parent !== window;
  function send(msg) {
    if (inShell) window.parent.postMessage(msg, "*");    // сообщение не секретное; каркас проверяет источник
  }
  window.SAMApp = {
    inShell,
    openPanel(opts) {
      const o = opts || {};
      send({ type: "sam:openPanel", title: o.title || "", path: o.path, html: o.html });
    },
    closePanel() { send({ type: "sam:closePanel" }); },
  };
})();
