// Команды по «/»: подсказки к набранному и разбор строки-команды
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {matchCommands, parseCommand, commandsHtml} from '../../../gateway/static/monitor/commands.js';

const COMMANDS = [{name: 'text-edit'}, {name: 'drift-guard'}, {name: 'git-style'}];
const names = list => list.map(c => c.name);

test('hints: prefix first, then containing; a phrase or typed arguments give none', () => {
  assert.deepEqual(names(matchCommands(COMMANDS, '/')), ['text-edit', 'drift-guard', 'git-style']);
  assert.deepEqual(names(matchCommands(COMMANDS, '/dr')), ['drift-guard']);
  assert.deepEqual(names(matchCommands(COMMANDS, '/edit')), ['text-edit']);
  assert.deepEqual(matchCommands(COMMANDS, 'привет'), []);
  assert.deepEqual(matchCommands(COMMANDS, '/drift-guard x'), []);
});

test('a command is a known name after the slash, a path is a phrase', () => {
  assert.deepEqual(parseCommand('/drift-guard начало задачи', COMMANDS), {name: 'drift-guard', args: 'начало задачи'});
  assert.equal(parseCommand('/путь/к/файлу', COMMANDS), null);
});

test('hint items: the selected one marked', () => {
  const html = commandsHtml([{name: 'a', source: 'skill'}, {name: 'b', source: 'x', description: '<d>'}], 1);
  assert.ok(html.includes('<div class="cmd-item" data-i="0">') && html.includes('<div class="cmd-item sel" data-i="1">'));
  assert.ok(html.includes('<span class="cmd-desc">&lt;d&gt;</span>'));
});
