/* Вкладка «Душа» панели: разделы характера SAM в хранилище (gateway/soul_api.py); инструкции SAM.md и личный слой
   data/SAM.md правятся файлами. Запись и откат - с ключом команд: ключа привилегий у страницы нет. Поле
   ключа очищается после отправки на шлюз, отмена его не трогает. Несохранённая правка не выбрасывается без вопроса. */

const SOUL_PREVIEW = 60;   // символов текста версии в строке истории
const soul = {name: '', version: 0, loaded: ''};   // раздел; версия, которую видит человек (запись поверх неё,
                                                   // изменилась - 409); текст, как пришёл со шлюза (отличие - правка)
// время из Mongo приходит без пояса, а пишется в UTC
const soulWhen = v => !v ? '?' : new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v : v + 'Z').toLocaleString();
const soulText = () => document.getElementById('soul-text');
const soulDirty = () => !!soul.name && soulText().value !== soul.loaded;

function soulMessage(text, isError) {
  const el = document.getElementById('soul-msg');
  el.textContent = text || '';
  el.classList.toggle('err', !!isError);
}

function soulFetch(url, options) {
  return fetch(url, options).then(r => r.json().then(d => {
    if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : (d.error || r.statusText));
    return d;
  }));
}

function soulKeepEdit(action) {
  return soulDirty() && !confirm(`В разделе ${soul.name} несохранённая правка. ${action} и выбросить её?`);
}

export function loadSoul() {
  soulFetch('/sam/soul').then(d => {
    const sel = document.getElementById('soul-section');
    sel.replaceChildren(...d.sections.map(s => {
      const o = document.createElement('option');
      o.value = s._id;
      o.textContent = s._id;
      return o;
    }));
    const keep = d.sections.some(s => s._id === soul.name) ? soul.name : (d.sections[0] || {})._id;
    if (!keep) return;
    sel.value = keep;
    if (keep === soul.name && soulKeepEdit('Перечитать раздел')) return;
    loadSoulSection(keep);
  }).catch(e => soulMessage(e.message, true));
}

export function pickSoulSection(sel) {
  if (soulKeepEdit('Перейти к разделу ' + sel.value)) { sel.value = soul.name; return; }
  loadSoulSection(sel.value);
}

function soulVersionRow(v, current) {
  const row = document.createElement('div');
  row.className = 'soul-ver';
  const num = document.createElement('span');
  num.className = 'sam-num';
  num.textContent = 'v' + v.version;
  const info = document.createElement('span');
  info.className = 'soul-info';
  info.textContent = `${soulWhen(v.updated_at)} · ${v.author || ''} · ${(v.text || '').slice(0, SOUL_PREVIEW)}`;
  info.title = v.text || '';
  row.append(num, info);
  if (current) {
    const mark = document.createElement('span');
    mark.textContent = 'текущая';
    row.append(mark);
  } else {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pill';
    btn.textContent = 'откатить';
    btn.onclick = () => rollbackSoulSection(v.version);
    row.append(btn);
  }
  return row;
}

// message - что сказать после загрузки (итог записи с предупреждением): загрузка его не затирает
function loadSoulSection(name, message, isError) {
  if (!name) return;
  const url = '/sam/soul/' + encodeURIComponent(name);
  Promise.all([soulFetch(url), soulFetch(url + '/history')]).then(([doc, hist]) => {
    soul.name = name;
    soul.loaded = doc.text || '';
    soulText().value = soul.loaded;
    soul.version = doc.version;
    document.getElementById('soul-meta').textContent = `версия ${doc.version} · ${soulWhen(doc.updated_at)} · ${doc.author || ''}`;
    document.getElementById('soul-history').replaceChildren(...hist.versions.map((v, i) => soulVersionRow(v, i === 0)));
    soulMessage(message, isError);
  }).catch(e => soulMessage(e.message, true));
}

function sendSoul(path, method, body, question) {
  const input = document.getElementById('soul-secret');
  const secret = input.value.trim();
  if (!secret) { soulMessage('Нужен ключ команд: впиши его в поле ключа', true); return; }
  if (!confirm(question)) return;
  input.value = '';
  soulFetch('/sam/soul/' + encodeURIComponent(soul.name) + path,
            {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...body, secret})})
    .then(d => loadSoulSection(soul.name, d.message))
    .catch(e => soulMessage(e.message, true));   // правка остаётся в редакторе: её можно перенести после перечитки
}

export function saveSoulSection() {
  sendSoul('', 'PUT', {text: soulText().value, expected_version: soul.version},
    `Записать раздел ${soul.name}? Правка дойдёт до SAM со следующего хода.`);
}

function rollbackSoulSection(version) {
  if (soulKeepEdit('Откатить раздел')) return;
  sendSoul('/rollback', 'POST', {version, expected_version: soul.version},
    `Откатить раздел ${soul.name} к версии ${version}? Откат станет новой версией.`);
}

export function discardSoulEdit() {
  if (soulDirty() && !confirm('Выбросить несохранённую правку?')) return;
  loadSoulSection(soul.name);
}
