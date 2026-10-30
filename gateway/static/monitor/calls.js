/* Вызовы инструментов в ленте: строка вызова в группе, итог под ней, пометка guard на строке. Пара вызов-итог
   находится по call_id, без него - по имени инструмента в порядке вызовов. */
import {state, callRows, guardMarks, pendingCalls, callByName} from './state.js';
import {append, blockRow} from './feed.js';
import {scrollLog} from './log.js';
import {fmtTs} from './format.js';
import {callLine, isErrorResult, resultRowHtml, orphanResultLine} from './blocks/call.js';
import {GUARD_BLOCK, guardMarkHtml} from './blocks/guard.js';

// Вызов, разобранный из текста ответа (agent_loop.py, fake_tcs): id text_<шаг>_<номер> повторяются от хода к ходу,
// проверка идёт до вызова - пометка ждёт свой вызов, к старой строке с тем же id не садится
export const TEXT_CALL_PREFIX = 'text_';
// проверка guard и сам вызов - в одном шаге цикла; пометка старше этого к вызову не относится
export const GUARD_WAIT_MS = 60000;

export const callKey = ev => `${ev.session || ''}|${ev.call_id || ''}`;
export function evTime(ev) { const t = Date.parse(ev.ts_iso || ''); return isNaN(t) ? Date.now() : t; }

export function appendCall(ev) {
  const spec = callLine(ev);
  const row = blockRow(fmtTs(ev), spec);
  const dotEl = row.querySelector('.ev-dot');
  if (ev.call_id) {
    const key = callKey(ev);
    callRows[key] = {row};
    const m = guardMarks[key];
    delete guardMarks[key];
    if (m && Math.abs(evTime(ev) - m.at) <= GUARD_WAIT_MS) setGuardMark(row, m);
  }

  // группа: итог встаёт внутрь, под строку вызова
  const group = document.createElement('div');
  group.className = 'ev-group';
  if (spec.debug) group.classList.add('ev-debug');
  if (ev.session) group.dataset.sess = ev.session;
  group.appendChild(row);
  append(group);

  // вызов из истории (снимок, порция старых) итога не дождётся: его итог не хранится, а новый живой итог того же
  // инструмента зацепился бы за самый старый вызов
  if (state.logTarget) return;
  const cid = ev.call_id || ev.name + '_' + state.evN;
  pendingCalls[cid] = {group, dotEl};
  if (!callByName[ev.name]) callByName[ev.name] = [];
  callByName[ev.name].push(cid);
}

export function resolveCall(ev) {
  const cid = ev.call_id;
  let entry = pendingCalls[cid];

  // итог без своего id - самый ранний ждущий вызов того же инструмента
  if (!entry && callByName[ev.name] && callByName[ev.name].length) {
    const fallbackId = callByName[ev.name].shift();
    entry = pendingCalls[fallbackId];
    if (entry) delete pendingCalls[fallbackId];
  }

  const isErr = isErrorResult(ev);
  delete callRows[callKey(ev)];

  if (entry) {
    entry.dotEl.className = `ev-dot ${isErr ? 'dot-red' : 'dot-gray'}`;
    delete pendingCalls[cid];
    const res = document.createElement('div');
    res.className = 'ev-result';
    res.innerHTML = resultRowHtml(ev);
    entry.group.appendChild(res);
  } else {
    append(blockRow(fmtTs(ev), orphanResultLine(ev), ev.session));
  }
  if (state.autoScroll) scrollLog();
}

// Пометка guard на строке вызова (agent_loop: вызов из ответа модели - сначала tool_call, потом проверка; вызов из
// текста - наоборот, пометка ждёт свой вызов в guardMarks)
function setGuardMark(row, m) {
  const body = row.querySelector('.ev-body');
  const old = body.querySelector('.guard-mark');
  if (old) old.outerHTML = guardMarkHtml(m); else body.insertAdjacentHTML('beforeend', guardMarkHtml(m));
  if (m.state === GUARD_BLOCK) row.querySelector('.ev-dot').className = 'ev-dot dot-red';
}

export function markGuard(cid, guardState, ev) {
  if (!cid) return;
  const key = callKey({session: ev.session, call_id: cid});
  const m = {state: guardState, level: ev.level, reason: ev.reason, at: evTime(ev)};
  if (!cid.startsWith(TEXT_CALL_PREFIX)) {   // вызов из ответа модели: строка уже есть
    if (callRows[key]) setGuardMark(callRows[key].row, m);
    return;
  }
  // блокировка вызова из текста: вызова не будет, есть строка блокировки
  if (guardState === GUARD_BLOCK) delete guardMarks[key]; else guardMarks[key] = m;
}

// ссылки на снятые со страницы строки: итог или пометка guard не должны искать их
export function pruneDetachedCalls() {
  for (const [k, c] of Object.entries(callRows)) if (!c.row.isConnected) delete callRows[k];
  for (const [k, c] of Object.entries(pendingCalls)) if (!c.group.isConnected) delete pendingCalls[k];
  for (const [n, ids] of Object.entries(callByName)) callByName[n] = ids.filter(id => pendingCalls[id]);
}

export function forgetGuardMarks(sess) {
  for (const k of Object.keys(guardMarks)) if (k.startsWith(`${sess}|`)) delete guardMarks[k];
}
