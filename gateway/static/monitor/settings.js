/* Вкладка «Настройки» панели: поля self_config SAM (gateway/self_config.py) - переключатели, выбор, ползунки, ввод. */
import {JSON_POST} from './api.js';
import {showHistoryOff} from './status.js';

// Подписи настроек SAM по-человечески; ключ self_config - мелко рядом
const SETTING_LABELS = {
  voice_speed: 'Скорость голоса', voice_pitch: 'Высота голоса', voice_volume: 'Громкость голоса',
  persona_warmth: 'Теплота тона', tts_enabled: 'Озвучивать ответы', tts_announce_chat: 'Озвучивать ответы из монитора',
  plan_critic_enabled: 'Проверять план агента', response_critic_enabled: 'Проверять ответ перед отдачей',
  debug_mode: 'Отладка: не сохранять историю', voice_gender: 'Род речи',
};
const CHOICE_LABELS = {female: 'женский', male: 'мужской'};
const RANGE_STEP = 0.05;
const RANGE_DIGITS = 2;
// включение, которое теряет данные, - только с подтверждением
const CONFIRM_ON = {debug_mode: 'Разговоры перестанут сохраняться в историю, пока режим включён. Включить?'};

const el = (tag, attrs, ...kids) => {
  const n = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([a, x]) => n.setAttribute(a, x)); n.append(...kids); return n;
};

function settingField(k, v, schema) {
  const label = SETTING_LABELS[k] || k;
  const key = el('span', {class: 'sam-key'}, k);
  if (typeof v === 'boolean') {
    // переключает только сам переключатель: щелчок или выделение текста подписи настройку не трогает
    const box = el('input', {type: 'checkbox', role: 'switch', 'aria-label': label});
    box.checked = v;
    box.onchange = () => {
      if (CONFIRM_ON[k] && box.checked && !confirm(CONFIRM_ON[k])) { box.checked = false; return; }
      tuneSetting(k, box.checked);
    };
    return el('div', {class: 'sam-switch', title: k}, el('span', {}, label, ' ', key), box);
  }
  const head = el('span', {class: 'sam-label'}, el('span', {}, label), key);
  const choices = (schema.choices || {})[k];
  if (choices) {
    const sel = el('select', {'aria-label': label}, ...choices.map(c => el('option', {value: c}, CHOICE_LABELS[c] || c)));
    sel.value = v;
    sel.onchange = () => tuneSetting(k, sel.value);
    return el('label', {class: 'sam-field'}, head, sel);
  }
  const lim = (schema.limits || {})[k];
  if (lim && typeof v === 'number') {
    const range = el('input', {type: 'range', min: lim[0], max: lim[1], step: RANGE_STEP, 'aria-label': label});
    range.value = v;
    const out = el('output', {class: 'sam-num'}, Number(v).toFixed(RANGE_DIGITS));
    range.oninput = () => { out.textContent = Number(range.value).toFixed(RANGE_DIGITS); };
    range.onchange = () => tuneSetting(k, parseFloat(range.value));
    return el('label', {class: 'sam-field'}, head, el('span', {class: 'sam-range'}, range, out));
  }
  const inp = el('input', {type: typeof v === 'number' ? 'number' : 'text', 'aria-label': label});
  inp.value = v;
  inp.onchange = () => tuneSetting(k, typeof v === 'number' ? parseFloat(inp.value) : inp.value);
  return el('label', {class: 'sam-field'}, head, inp);
}

export function loadSettings() {
  Promise.all([fetch('/sam/config').then(r => r.json()),
               fetch('/sam/config/schema').then(r => r.json()).catch(() => ({}))]).then(([cfg, schema]) => {
    showHistoryOff(cfg.debug_mode);
    // переключатели после ползунков и выбора: сначала голос, потом поведение
    const entries = Object.entries(cfg).sort(([, a], [, b]) => (typeof a === 'boolean') - (typeof b === 'boolean'));
    document.getElementById('settings-list').replaceChildren(...entries.map(([k, v]) => settingField(k, v, schema)));
  });
}

function tuneSetting(key, value) {
  fetch('/sam/config/tune', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key, value}),
  }).then(r => r.json()).then(d => {
    if (!d.ok) console.warn('tune failed:', d.error);
    if (d.config) showHistoryOff(d.config.debug_mode);
  });
}

export function resetSettings() {
  if (!confirm('Сбросить все настройки SAM к значениям по умолчанию?')) return;
  fetch('/sam/config/reset', JSON_POST)
    .then(() => loadSettings());
}
