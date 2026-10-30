/* Разбор событий шлюза (WebSocket /ws/monitor и порции истории): снимок ленты, отсев уже показанного, счётчик и
   раздача по типу события. HTML строк собирают блоки (blocks/), вставку - лента (feed.js). */
import {state, busySessions} from './state.js';
import {DEFAULT_SESSION_ID} from './ids.js';
import {logEl, olderMark, scrollLog, enter, removeEmpty, ENTER_MAX_STEPS} from './log.js';
import {append, addDiv, blockRow, markIgnored, resetFeed, dropTransient} from './feed.js';
import {ensureTaskSection, updateTaskStatus} from './sections.js';
import {appendCall, resolveCall, markGuard} from './calls.js';
import * as live from './live-rows.js';
import {countEvent, setMainModel, refreshCompression} from './status.js';
import {addTask, setTaskStatus, updateTaskStage} from './tasks-model.js';
import {renderTasks, loadTasks} from './tasks-panel.js';
import {trackSess, trackActiveTask, renderSessionSelect} from './sessions.js';
import {forgetSession} from './session-actions.js';
import {setView} from './view.js';
import {loadCommands} from './composer.js';
import {fmtTs, clockNow} from './format.js';
import {TASK_PREFIX, taskSession} from './ids.js';
import {userLine} from './blocks/user.js';
import {pendingFeed} from './pending-feed.js';
import {replyLine, spokenLine} from './blocks/reply.js';
import {workerPlanLine} from './blocks/call.js';
import {GUARD_OK, GUARD_WAIT, GUARD_BLOCK, guardBlockLine} from './blocks/guard.js';
import {taskCreatedLine, stageLine, taskMessageLine, taskMessageStatus} from './blocks/agent.js';
import {llmRequestLine, LLM_ROW_TITLE, toggleMark} from './blocks/llm.js';
import {compressionErrorLine, compressionWaitingLine} from './blocks/compression.js';
import {sandboxLine, appLine} from './blocks/sandbox.js';
import {STT_DONE, isListening} from './blocks/voice.js';
import {loopGuardLine, criticLine, thinkingLine, injectedLine, dreamLine, workflowLine, gapLine}
  from './blocks/notice.js';

// типы, которые шлюз пишет в историю (event_bus _PERSIST) и отдаёт снимком; дельты потока и прочее сюда не идут
const SEEN_TYPES = new Set(['user_message', 'assistant_reply', 'assistant_spoken', 'tool_call', 'context_compressed']);
const seenMids = new Set();   // msg_id событий ленты: снимок после обрыва дописывает только новые
// ответ голосом об итоге задачи несёт её id: озвученное - итог в шапке агента
const TASK_DONE_RE = /\[TASK_DONE:([a-f0-9]{6,})\]/;
const MUTED_REPLY_STYLE = 'opacity:0.55';
// вызовы сводки (SAM-97) - не строкой каждый, их ход показывает compression_progress
const SUMMARIZE_ROLE = 'summarize';

const snap = {
  live: false,        // лента уже собрана из снимка: следующий снимок - переподключение
  overlap: false,     // снимок переподключения задел уже показанное - дыры между ними нет
  viewRestored: false,   // лента из history.state после F5 - один раз, по первому снимку
  bootView: null,     // лента из history.state при загрузке страницы (main.js читает её до каркаса)
};
export const setBootView = view => { snap.bootView = view; };

const shell = () => window.SAMShell;
// строка блока: в сессию события или в явно названную (агента, без сессии), с msg_id записи истории
const add = (ev, spec) => append(blockRow(fmtTs(ev), spec, ev.session));
const addTo = (ev, spec, sess, mid) => append(blockRow(fmtTs(ev), spec, sess, mid));

/* ── Снимок ── */
function snapshotBegin() {
  state.feedGen++;
  if (snap.live) dropTransient(); else { resetFeed(); seenMids.clear(); }
  snap.overlap = false;
  state.logTarget = document.createDocumentFragment();
}

function snapshotEnd(ev) {
  const log = logEl();
  const frag = state.logTarget;
  state.logTarget = null;
  if (!snap.live) {
    const first = !state.paintedFromCache && !log.querySelector('.ev');
    // пустой снимок (база недоступна) ленту не трогает: остаются кадр из кеша или «жду событий», и следующий
    // снимок снова заменит ленту целиком
    if (frag.childNodes.length) {
      log.replaceChildren(olderMark(), frag);
      state.oldestIso = ev.oldest || ''; state.oldestId = ev.oldest_id || ''; state.olderDone = false;
      // первая загрузка - одна сцена: последние сообщения встают снизу вверх
      if (first) [...log.children].slice(-ENTER_MAX_STEPS).reverse().forEach((row, k) => enter(row, k));
      snap.live = true;
    }
  } else {
    // обрыв дольше порции снимка: ни одно его событие лента не видела - между ними могла остаться дыра
    if (ev.full && !snap.overlap) frag.prepend(blockRow(clockNow(), gapLine()));
    if (frag.childNodes.length) { removeEmpty(); log.appendChild(frag); }
  }
  state.paintedFromCache = false;
  if (state.autoScroll && state.atBottom) scrollLog();
  loadTasks();   // шапки агентов и панель задач - из sam.tasks: событий task_* в истории нет
  if (!snap.viewRestored && snap.live) {
    snap.viewRestored = true;
    if (snap.bootView) {
      history.replaceState({monView: snap.bootView}, '');   // запись истории снова знает свою ленту
      setView(snap.bootView, true);
    }
  }
}

// что лента уже видела (тот же msg_id у живого события и у записи истории), не повторяется: снимок после обрыва
// и события, пришедшие в очередь, пока шлюз строил снимок
function alreadyShown(ev) {
  if (!(SEEN_TYPES.has(ev.type) && ev.msg_id && !state.olderBusy)) return false;
  if (seenMids.has(ev.msg_id)) { if (state.logTarget) snap.overlap = true; return true; }
  seenMids.add(ev.msg_id);
  return false;
}

/* ── Задачи агентов ── */
function taskEnded(ev, status, extra) {
  if (setTaskStatus(ev.task_id, status)) renderTasks();
  loadTasks();   // итог и причина - в записи задачи
  if (ev.task_id) {
    updateTaskStatus(ev.task_id, {status, stage: '', ...extra});
    renderSessionSelect();   // закончившийся агент уходит из селектора, если его не выбрали
  }
}

// Запрос к модели - отладочная строка, раскрывается по клику на шапку
function showLlmRequest(ev) {
  const row = blockRow(fmtTs(ev), llmRequestLine(ev), ev.session, ev.msg_id);
  row.classList.add('ev-debug');
  row.title = LLM_ROW_TITLE;
  row.classList.add('llm-req');
  row.querySelector('.llm-req-head').addEventListener('click', () => {
    const open = row.classList.toggle('open');
    row.querySelector('.llm-req-head .toggle').textContent = toggleMark(open);
  });
  append(row);
}

const HANDLERS = {
  user_message(ev) {
    if (ev.system === true) return;
    if (shell() && !ev.replay) shell().setVoice(false);
    live.dropVoiceState(ev);   // пришло реальное сообщение - строка «слушаю / распознаю» уходит
    const row = markIgnored(blockRow(fmtTs(ev), userLine(ev), ev.session, ev.msg_id), ev);
    if (pendingFeed.settle(ev, row)) return;   // SAM-241 (флаг sam.monitor.pending): «в очереди» -> настоящая строка на том же месте
    addDiv();
    append(row);
  },
  session_deleted(ev) { if (ev.session_id) forgetSession(ev.session_id); },
  main_model_switch(ev) {
    setMainModel(ev.to);
    loadCommands();   // SAM-99: у новой основной модели свои команды
  },
  history_ignored(ev) { live.markIgnoredIds(ev.msg_ids); },
  tool_call: appendCall,
  tool_result: resolveCall,

  task_created(ev) {
    ensureTaskSection(ev.task_id, ev.description);
    if (ev.name) updateTaskStatus(ev.task_id, {name: ev.name});
    trackActiveTask(ev.task_id);   // агент - адресат в селекторе
    addTo(ev, taskCreatedLine(ev), ev.task_id ? `${TASK_PREFIX}${ev.task_id}` : undefined);
    addTask(ev);
    renderTasks();
  },
  task_message(ev) {
    if (ev.task_id) updateTaskStatus(ev.task_id, taskMessageStatus(ev.content));
    addTo(ev, taskMessageLine(ev), ev.task_id ? `${TASK_PREFIX}${ev.task_id}` : ev.session);
  },
  stage_update(ev) {
    if (ev.task_id) updateTaskStatus(ev.task_id, {stage: `${ev.name || ''} ${ev.status || ''}`.trim()});
    addTo(ev, stageLine(ev), ev.task_id ? taskSession(ev.task_id) : undefined);
    if (updateTaskStage(ev)) renderTasks();
  },
  task_cancelled: ev => taskEnded(ev, 'cancelled'),
  task_done: ev => taskEnded(ev, 'done'),
  task_failed: ev => taskEnded(ev, 'failed', {final: ev.reason || 'failed'}),

  assistant_delta: live.streamDelta,
  assistant_stream_reset: live.streamReset,
  assistant_stream_end: live.streamEnd,
  assistant_reply(ev) {
    live.dropStream(ev);
    const td = (ev.content || '').match(TASK_DONE_RE);
    if (td && ev.voice) updateTaskStatus(td[1], {final: ev.voice});
    const spec = replyLine(ev);
    const row = blockRow(fmtTs(ev), spec, ev.session, ev.msg_id);
    if (spec.muted) row.style.cssText += ';' + MUTED_REPLY_STYLE;
    markIgnored(row, ev);
    // Голосовой ход (ADR-013): ответ ждёт отчёта моста о судьбе, значок обновит assistant_spoken
    if (ev.system === true && ev.voice) live.awaitFate(row, ev.voice);
    append(row);
    addDiv();
  },
  assistant_spoken(ev) {
    // Прозвучавшее (ADR-013): судьба - поле события, значок у ответа хода; ответа в ленте нет - своя строка
    if (live.settleFate(ev)) return;
    addTo(ev, spokenLine(ev), ev.session, ev.msg_id);
    addDiv();
  },

  loop_guard: ev => add(ev, loopGuardLine(ev)),
  voice_state(ev) {
    if (ev.state === STT_DONE) return;
    if (shell()) shell().setVoice(isListening(ev.state));
    live.showVoiceState(ev);
  },
  session_busy(ev) {
    busySessions.add(ev.session || DEFAULT_SESSION_ID);
    if (shell()) shell().setBusy(true);
    live.showBusy(ev);
  },
  session_idle(ev) {
    busySessions.delete(ev.session || DEFAULT_SESSION_ID);
    if (shell()) shell().setBusy(busySessions.size > 0);
    live.dropBusy(ev);
  },
  injected: ev => add(ev, injectedLine(ev)),
  worker_plan: ev => addTo(ev, workerPlanLine(ev), ev.session, ev.msg_id),

  // проверка guard - пометка на строке вызова; отдельной строкой - только блокировка
  guard_check: ev => markGuard(ev.call_id, GUARD_WAIT, ev),
  guard_pass: ev => markGuard(ev.call_id, GUARD_OK, ev),
  guard_block(ev) {
    markGuard(ev.call_id, GUARD_BLOCK, ev);
    add(ev, guardBlockLine(ev));
  },
  critic_check: ev => add(ev, criticLine(ev)),
  critic_pass: ev => add(ev, criticLine(ev)),
  critic_block: ev => add(ev, criticLine(ev)),
  dream: ev => add(ev, dreamLine(ev)),
  workflow: ev => add(ev, workflowLine(ev)),

  // После F5 ошибку сжатия показывает шапка из /health (SAM-97)
  compression_error(ev) {
    add(ev, compressionErrorLine(ev));
    live.compressionProgressDone(ev.session, ev.error);
    refreshCompression();
  },
  compression_waiting(ev) {
    add(ev, compressionWaitingLine(ev));
    refreshCompression();
  },
  compression_progress: live.compressionProgress,
  context_compressed(ev) {
    if (!ev.replay) { refreshCompression(); live.compressionProgressDone(ev.session, null); }
    if (ev.replay) live.appendSummaries(ev); else live.replaceWithSummaries(ev);
  },

  sandbox_result: ev => add(ev, sandboxLine(ev)),
  app_result: ev => add(ev, appLine(ev)),
  llm_request(ev) { if (ev.role !== SUMMARIZE_ROLE) showLlmRequest(ev); },
  thinking(ev) {
    const r = blockRow(fmtTs(ev), thinkingLine(ev), ev.session);
    r.classList.add('ev-debug');
    append(r);
  },
};

export function handle(ev) {
  if (ev.type === 'snapshot_begin') { snapshotBegin(); return; }
  if (ev.type === 'snapshot_end') { snapshotEnd(ev); return; }
  if (alreadyShown(ev)) return;
  removeEmpty();
  state.evN++;
  countEvent(state.evN, !state.logTarget);
  if (ev.session) trackSess(ev.session);
  const handler = HANDLERS[ev.type];
  if (handler) handler(ev);
}
