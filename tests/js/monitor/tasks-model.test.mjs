// Задачи как данные: живость, шаги плана, слияние с /sam/tasks, архив панели
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {taskLive, addTask, mergeTaskList, updateTaskStage, setTaskStatus, findTask, taskBoard}
  from '../../../gateway/static/monitor/tasks-model.js';
import {tasks, taskInfo, clearAll} from '../../../gateway/static/monitor/state.js';

const HOUR_MS = 3600 * 1000;
const fresh = () => clearAll(tasks, taskInfo);

test('a task with stages: all done - done, one failed - failed', () => {
  fresh();
  addTask({task_id: 't1', description: 'd', stages: [{name: 'a'}, {name: 'b'}]});
  assert.ok(taskLive('t1'));
  updateTaskStage({task_id: 't1', idx: 0, status: 'done'});
  assert.equal(tasks.t1.status, 'running');
  updateTaskStage({task_id: 't1', idx: 1, status: 'skipped'});
  assert.equal(tasks.t1.status, 'done');
  assert.ok(!taskLive('t1'));
  assert.equal(updateTaskStage({task_id: 'none'}), false);
});

test('the task record is the truth after F5', () => {
  fresh();
  mergeTaskList([{task_id: 't2', status: 'waiting', name: 'n', error: 'boom', created_at: '2026-10-15T00:00:00Z'}]);
  assert.equal(tasks.t2.result, 'boom');
  assert.ok(taskLive('t2'));
  assert.ok(setTaskStatus('t2', 'cancelled') && !taskLive('t2'));
  assert.equal(setTaskStatus('none', 'done'), false);
  assert.equal(findTask('t2-long-id').id, 't2');
});

test('finished tasks older than three hours go to the archive', () => {
  fresh();
  const now = Date.parse('2026-10-15T12:00:00Z');
  mergeTaskList([
    {task_id: 'old', status: 'done', created_at: new Date(now - 4 * HOUR_MS).toISOString()},
    {task_id: 'new', status: 'done', created_at: new Date(now - HOUR_MS).toISOString()},
    {task_id: 'live', status: 'running', created_at: new Date(now - 5 * HOUR_MS).toISOString()},
  ]);
  const board = taskBoard(now, false);
  assert.deepEqual(board.archived.map(t => t.id), ['old']);
  assert.deepEqual(board.shown.map(t => t.id), ['new', 'live']);
  assert.deepEqual(taskBoard(now, true).shown.map(t => t.id), ['new', 'old', 'live']);
});
