/* Строка ввода: реплика, команда по «/», вставка в агента; история отправленного стрелками; Esc - снять последнее
   из очереди, Ctrl/Shift+Esc - прервать ход, Ctrl+Z - убрать последний обмен из истории. */
import {JSON_POST, getJson} from './api.js';
import {append, addDiv, blockRow} from './feed.js';
import {removeEmpty} from './log.js';
import {clockNow} from './format.js';
import {isTaskSession} from './ids.js';
import {CMD_PREFIX, matchCommands, parseCommand, commandsHtml} from './commands.js';
import {echoLine, imagesPreview, pendingLine, pendingFailedLine} from './blocks/user.js';
import {pendingFeed} from './pending-feed.js';
import {popLastLine, cancelTurnLine, undoLine, commandFailedLine, reanimatedLine, injectFailedLine}
  from './blocks/action.js';
import {hasImages, takeImages} from './attachments.js';
import {getSess, loadSessions} from './sessions.js';

const SENT_HISTORY_MAX = 50;      // отправленного для стрелки вверх - как в shell
const FLASH_MS = 900;             // красная черта под полем: реплика не ушла
const sent = {list: [], cursor: -1};   // cursor -1 - не в истории

const inp = () => document.getElementById('inp');
const getVal = () => inp().value.trim();
const clearVal = () => { inp().value = ''; };

export function flash() {
  const el = inp();
  el.style.borderBottomColor = 'var(--red)';
  setTimeout(() => el.style.borderBottomColor = '', FLASH_MS);
}

// адресата ещё нет (шлюз не ответил на /sam/sessions): реплика не уходит, список запрашивается снова
function targetKnown() {
  if (getSess()) return true;
  flash();
  loadSessions();
  return false;
}

function remember(msg) {
  sent.list.push(msg);
  if (sent.list.length > SENT_HISTORY_MAX) sent.list.shift();
  sent.cursor = -1;
}

/* ── Команды по «/» (SAM-99) ── */
const cmd = {list: [], matches: [], sel: 0};

export function loadCommands() {
  getJson('/sam/commands').then(d => { cmd.list = d.commands || []; renderCmdPop(); })
    .catch(() => {});
}

function renderCmdPop() {
  const pop = document.getElementById('cmd-pop');
  cmd.matches = matchCommands(cmd.list, inp().value);
  if (cmd.sel >= cmd.matches.length) cmd.sel = 0;
  pop.classList.toggle('open', cmd.matches.length > 0);
  pop.innerHTML = commandsHtml(cmd.matches, cmd.sel);
}

function pickCommand(i) {
  const c = cmd.matches[i];
  if (!c) return;
  inp().value = CMD_PREFIX + c.name + ' ';
  inp().focus();
  renderCmdPop();
}

// Клавиши подсказки: ↑/↓ - выбор, Tab или Enter - подставить, Esc - закрыть. true - клавиша съедена
function cmdKey(e) {
  if (!cmd.matches.length) return false;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    cmd.sel = (cmd.sel + (e.key === 'ArrowDown' ? 1 : cmd.matches.length - 1)) % cmd.matches.length;
  } else if (e.key === 'Tab' || e.key === 'Enter') {
    pickCommand(cmd.sel);
  } else if (e.key === 'Escape') {
    cmd.matches = [];
    document.getElementById('cmd-pop').classList.remove('open');
    e.preventDefault();
    return true;
  } else {
    return false;
  }
  e.preventDefault();
  renderCmdPop();
  return true;
}

// Команда уходит основной модели командой (POST /sam/session/{id}/command); строку в ленту кладёт шлюз
function sendCommand(device, command) {
  fetch(`/sam/session/${encodeURIComponent(device)}/command`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(command),
  }).then(r => r.json().then(d => {
    if (r.ok) return;
    append(blockRow(clockNow(), commandFailedLine(d.detail || r.status), device));
  })).catch(() => flash());
}

/* ── Отправка ── */
export function doSend() {
  const msg = getVal();
  if (!msg && !hasImages()) return;
  if (!targetKnown()) return;
  const device = getSess();
  const command = (!isTaskSession(device) && !hasImages()) ? parseCommand(msg, cmd.list) : null;
  clearVal();
  renderCmdPop();
  if (command) {
    remember(msg);
    sendCommand(device, command);
    return;
  }

  const images = takeImages();
  if (msg) remember(msg);

  removeEmpty();
  addDiv();
  const now = clockNow();
  const isTaskInject = isTaskSession(device);
  const imgPreview = imagesPreview(images);
  // SAM-88: набранный текст сам себя не показывает - в нём может быть секрет команд. Текст приходит
  // от сервера уже с маской: чат - событием user_message, inject - ответом /sam/inject
  const echo = text => append(blockRow(now, echoLine(text, isTaskInject, imgPreview), device));
  // SAM-241 (флаг sam.monitor.pending, выкл): реплика видна сразу с пометкой «в очереди», user_message её заменит
  const pendingId = isTaskInject ? null : pendingFeed.begin(device, () => blockRow(now, pendingFailedLine(imgPreview), device));
  if (pendingId) {
    const el = blockRow(now, pendingLine(imgPreview), device);
    pendingFeed.attach(pendingId, el);
    append(el);
  } else if (!isTaskInject && images.length) echo('');

  if (isTaskInject) sendInject(device, msg, images, echo);
  else sendChat(device, msg, images, pendingId);
}

function sendInject(device, msg, images, echo) {
  fetch('/sam/inject', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: device, message: msg, images: images.map(i => i.dataUrl)}),
  }).then(r => r.json()).then(d => {
    echo(d.message || '');
    if (d.kind === 'reanimated') append(blockRow(clockNow(), reanimatedLine(device), device));
    else if (d.status === 'error') append(blockRow(clockNow(), injectFailedLine(d), device));
  }).catch(() => flash());
}

// с картинками - multimodal content (формат OpenAI vision)
function sendChat(device, msg, images, pendingId) {
  let userContent;
  if (images.length) {
    userContent = [];
    if (msg) userContent.push({type: 'text', text: msg});
    for (const img of images) {
      userContent.push({type: 'image_url', image_url: {url: img.dataUrl}});
    }
  } else {
    userContent = msg;
  }
  fetch('/v1/chat/completions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Device-ID': device},
    body: JSON.stringify({messages: [{role: 'user', content: userContent}]}),
  }).then(r => { if (!r.ok) pendingFeed.fail(pendingId); }).catch(() => { pendingFeed.fail(pendingId); });
}

// действие над очередью или историей адресата: ответ шлюза - строкой в ленту после разделителя
function sessionAction(path, toLine) {
  if (!targetKnown()) return;
  const device = getSess();
  fetch(`/sam/session/${encodeURIComponent(device)}/${path}`, JSON_POST)
    .then(r => r.json())
    .then(d => {
      const t = clockNow();
      removeEmpty(); addDiv();
      append(blockRow(t, toLine(d, device)));
    })
    .catch(() => flash());
}
const doPopLast = () => sessionAction('pop-last', popLastLine);
const doCancelTurn = () => sessionAction('cancel-turn', cancelTurnLine);
const doUndoLast = () => sessionAction('undo-last', undoLine);

/* ── Клавиши ── */
function onKey(e) {
  const field = e.target;
  if (cmdKey(e)) return;
  if (e.key === 'Enter') {
    e.preventDefault();
    doSend();
  } else if (e.key === 'Escape') {
    e.preventDefault();
    if (field.value) {
      clearVal();
      sent.cursor = -1;
    } else if (e.ctrlKey || e.shiftKey) {
      doCancelTurn();
    } else {
      doPopLast();
    }
  } else if (e.key === 'z' && e.ctrlKey) {
    e.preventDefault();
    doUndoLast();
  } else if (e.key === 'ArrowUp') {
    if (sent.list.length === 0) return;
    e.preventDefault();
    if (sent.cursor === -1) sent.cursor = sent.list.length - 1;
    else if (sent.cursor > 0) sent.cursor--;
    field.value = sent.list[sent.cursor];
    setTimeout(() => field.setSelectionRange(field.value.length, field.value.length), 0);   // курсор в конец
  } else if (e.key === 'ArrowDown') {
    if (sent.cursor === -1) return;
    e.preventDefault();
    if (sent.cursor < sent.list.length - 1) {
      sent.cursor++;
      field.value = sent.list[sent.cursor];
    } else {
      sent.cursor = -1;
      field.value = '';
    }
  }
}

export function watchComposer() {
  inp().addEventListener('input', () => { cmd.sel = 0; renderCmdPop(); });
  document.getElementById('cmd-pop').addEventListener('mousedown', e => {
    const item = e.target.closest('.cmd-item');
    if (item) { e.preventDefault(); pickCommand(Number(item.dataset.i)); }
  });
  inp().addEventListener('keydown', onKey);
}
