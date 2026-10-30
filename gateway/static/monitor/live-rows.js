/* Строки, которые меняются на месте: живая печать потока, состояние голоса, «SAM думает», судьба озвучки ответа,
   ход сжатия, сводка вместо сжатых сообщений и пометка «модель не видит». */
import {compressionRows} from './state.js';
import {DEFAULT_SESSION_ID, STREAM_ROW_PREFIX, BUSY_ROW_PREFIX} from './ids.js';
import {append, blockRow, markIgnored} from './feed.js';
import {logEl} from './log.js';
import {fmtTs} from './format.js';
import {streamLine, fateIcon, fateHead} from './blocks/reply.js';
import {voiceStateLine, busyLine} from './blocks/voice.js';
import {progressLine, compressionProgressHtml, compressionDone, summaryLine, summaryHtml, SUMMARY_MARK, SUMMARY_DOT}
  from './blocks/compression.js';

const VOICE_SESSION_ID = 'voice';
// замена сжатых строк сводкой: сводка проявляется лесенкой, снятые строки гаснут
const SUMMARY_FADE = 'opacity 0.4s', SUMMARY_SHOW_MS = 50, SUMMARY_STEP_MS = 80;
const DROP_FADE = 'opacity 0.25s', DROP_REMOVE_MS = 260;

/* ── Поток печати (SAM-09): одна строка stream-<сессия> до прихода полного ответа ── */
const streamId = ev => `${STREAM_ROW_PREFIX}${ev.session || DEFAULT_SESSION_ID}`;

export function streamDelta(ev) {
  const sid = ev.session || DEFAULT_SESSION_ID;
  const rowId = streamId(ev);
  let row = document.getElementById(rowId);
  if (!row) {
    row = blockRow(fmtTs(ev), streamLine(), sid, rowId);
    row.id = rowId;
    append(row);
  }
  const buf = row.querySelector('.stream-buf');
  if (buf) buf.textContent += (ev.text || '');
}

// SAM-73: стрим оборвался, запасной поставщик начнёт ответ заново - показанное начало стирается, текст не задваивается
export function streamReset(ev) {
  const row = document.getElementById(streamId(ev));
  if (row) {
    const buf = row.querySelector('.stream-buf');
    if (buf) buf.textContent = '';
  }
}

// конец потока: курсор снимается, полный assistant_reply придёт следом и заменит строку
export function streamEnd(ev) {
  const row = document.getElementById(streamId(ev));
  if (row) {
    const cur = row.querySelector('.stream-cursor');
    if (cur) cur.remove();
  }
}

// финальный ответ: плейсхолдер потока (в нём могло быть сырьё <think>) уходит, остаётся одна итоговая строка
export function dropStream(ev) {
  const row = document.getElementById(streamId(ev));
  if (row) row.remove();
}

/* ── Голос: одна строка состояния на сессию ── */
const voiceStateId = ev => `voice-state-${ev.session || VOICE_SESSION_ID}`;

export function showVoiceState(ev) {
  const spec = voiceStateLine(ev);
  const existing = document.getElementById(voiceStateId(ev));
  if (existing) {
    const dot = existing.querySelector('.ev-dot');
    const body = existing.querySelector('.ev-body');
    if (dot) dot.className = `ev-dot ${spec.dot}`;
    if (dot) dot.textContent = spec.mark;
    if (body) body.innerHTML = spec.html;
    const ts = existing.querySelector('.ev-ts');
    if (ts) ts.textContent = fmtTs(ev);
  } else {
    const row = blockRow(fmtTs(ev), spec, ev.session);
    row.id = voiceStateId(ev);
    append(row);
  }
}

// пришла реплика - висящая строка «слушаю / распознаю» уходит
export function dropVoiceState(ev) {
  const row = document.getElementById(voiceStateId(ev));
  if (row) row.remove();
}

/* ── «SAM думает» с кнопкой прервать - пока идёт ход ── */
export function showBusy(ev) {
  const row = blockRow(fmtTs(ev), busyLine(ev), ev.session, `busy-${ev.session}`);
  row.id = `${BUSY_ROW_PREFIX}${ev.session}`;
  append(row);
}

export function dropBusy(ev) {
  const el = document.getElementById(`${BUSY_ROW_PREFIX}${ev.session}`);
  if (el) el.remove();
}

/* ── Судьба озвучки (ADR-013): ответ голосового хода ждёт отчёта моста ── */
export function awaitFate(row, voice) {
  row.dataset.fatePending = '1';
  row.dataset.voice = voice;
}

// Самый старый ответ сессии, ждущий отчёта, чья речь начинается с прозвучавшего
function findFatePending(sess, voice) {
  const head = fateHead(voice);
  for (const row of document.querySelectorAll('.ev[data-fate-pending]')) {
    if (row.dataset.sess === sess && (row.dataset.voice || '').trim().startsWith(head)) return row;
  }
  return null;
}

// значок судьбы у ждущего ответа; false - такого ответа в ленте нет
export function settleFate(ev) {
  const row = findFatePending(ev.session || DEFAULT_SESSION_ID, ev.voice || '');
  if (!row) return false;
  delete row.dataset.fatePending;
  row.querySelectorAll('.fate-icon').forEach(el => { el.outerHTML = fateIcon(ev.fate); });
  return true;
}

/* ── Ход сжатия (SAM-97): одна строка на сессию, закрывается context_compressed или compression_error ── */
export function compressionProgress(ev) {
  const sid = ev.session || '';
  let entry = compressionRows[sid];
  if (!entry) {
    entry = compressionRows[sid] = {row: blockRow(fmtTs(ev), progressLine(), sid), calls: 0};
    append(entry.row);
  }
  entry.calls = ev.call;
  entry.row.querySelector('.ev-body').innerHTML = compressionProgressHtml(ev);
}

export function compressionProgressDone(sid, error) {
  const entry = compressionRows[sid || ''];
  if (!entry) return;
  delete compressionRows[sid || ''];
  const done = compressionDone(sid, error, entry.calls);
  entry.row.querySelector('.ev-dot').className = `ev-dot ${done.dot}`;
  entry.row.querySelector('.ev-body').innerHTML = done.html;
}

/* ── Сводка сжатия ── */
// снимок (F5): каждая сводка - своя строка с её agg_id
export function appendSummaries(ev) {
  for (const s of ev.summaries || []) {
    append(markIgnored(blockRow(s.time_range || ev.ts, summaryLine(s), ev.session, s.agg_id), s));
  }
}

// живое сжатие - как update_one в MongoDB: первая строка чанка становится сводкой, остальные уходят
export function replaceWithSummaries(ev) {
  const log = logEl();
  (ev.summaries || []).forEach((s, i) => {
    const aggRow = s.agg_id ? log.querySelector(`[data-mid="${s.agg_id}"]`) : null;
    if (aggRow) {
      aggRow.querySelector('.ev-ts').textContent = s.time_range || ev.ts;
      aggRow.querySelector('.ev-dot').textContent = SUMMARY_MARK;
      aggRow.querySelector('.ev-dot').className = `ev-dot ${SUMMARY_DOT}`;
      aggRow.querySelector('.ev-body').innerHTML = summaryHtml(s);
      aggRow.style.transition = SUMMARY_FADE;
      aggRow.style.opacity = '0';
      setTimeout(() => { aggRow.style.opacity = '1'; }, SUMMARY_SHOW_MS + i * SUMMARY_STEP_MS);
    }
    (s.del_ids || []).forEach(mid => {
      const el = mid ? log.querySelector(`[data-mid="${mid}"]`) : null;
      if (el) { el.style.transition = DROP_FADE; el.style.opacity = '0'; setTimeout(() => el.remove(), DROP_REMOVE_MS); }
    });
  });
}

// SAM-92: записи, которые модель больше не видит
export function markIgnoredIds(ids) {
  for (const id of ids || []) {
    document.querySelectorAll(`.ev[data-mid="${CSS.escape(id)}"]`).forEach(r => r.classList.add('ignored'));
  }
}
