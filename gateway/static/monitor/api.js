/* Запросы к шлюзу, общие для модулей страницы. */

// POST к шлюзу - только JSON (SAM-188): у ручек без тела тоже заголовок и пустой объект
export const JSON_POST = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'};

export const getJson = url => fetch(url).then(r => r.json());
