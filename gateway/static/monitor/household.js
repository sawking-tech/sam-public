/* Вкладка «Дом» панели: анкета людей и питомцев (gateway/household_api.py). Состав попадает в промпт SAM.
   Заполнять необязательно: пустой дом SAM спросит сама в разговоре. */

const API = '/sam/household';
const KINDS = [['person', 'Человек'], ['pet', 'Питомец']];
const listEl = () => document.getElementById('household-list');

function message(text, isError) {
  const el = document.getElementById('household-msg');
  el.textContent = text || '';
  el.classList.toggle('err', !!isError);
}

function request(url, options) {
  return fetch(url, options).then(r => r.json().then(d => {
    if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : r.statusText);
    return d;
  }));
}

function input(label, value, maxLength) {
  const el = document.createElement('input');
  el.className = 'sam-input';
  el.placeholder = label;
  el.setAttribute('aria-label', label);
  el.maxLength = maxLength;
  el.value = value || '';
  return el;
}

function button(text, onClick, extra) {
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'sam-btn' + (extra ? ' ' + extra : '');
  el.textContent = text;
  el.addEventListener('click', onClick);
  return el;
}

function memberRow(member) {
  const row = document.createElement('div');
  row.className = 'household-row';
  const kind = document.createElement('select');
  kind.className = 'sam-input';
  kind.setAttribute('aria-label', 'Кто это');
  kind.replaceChildren(...KINDS.map(([value, title]) => new Option(title, value)));
  kind.value = member.kind || 'person';
  const name = input('Имя', member.name, 60);
  const role = input('Роль в доме', member.role, 60);
  const about = input('Коротко о человеке', member.about, 300);
  const actions = document.createElement('div');
  actions.className = 'row';
  const save = () => request(API, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({kind: kind.value, name: name.value, role: role.value, about: about.value}),
  }).then(() => { message('Сохранено'); return loadHousehold(); }).catch(e => message(e.message, true));
  const remove = () => {
    if (!member.key) { row.remove(); return; }
    request(`${API}/${encodeURIComponent(member.kind)}/${encodeURIComponent(member.key)}`, {method: 'DELETE'})
      .then(() => { message('Удалено'); return loadHousehold(); }).catch(e => message(e.message, true));
  };
  actions.append(button('Сохранить', save, 'primary'), button('Удалить', remove));
  row.append(kind, name, role, about, actions);
  return row;
}

export function addHouseholdMember() {
  listEl().append(memberRow({}));
}

export function loadHousehold() {
  return request(API).then(d => {
    listEl().replaceChildren(...d.members.map(memberRow));
    if (!d.members.length) addHouseholdMember();
  }).catch(e => message(e.message, true));
}
