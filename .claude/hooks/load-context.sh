#!/bin/bash
# load-context.sh — инжектит контекст предыдущей сессии + git-дифф с момента сохранения.
# Если контекста нет — показывает git log как fallback.
# Срабатывает один раз за сессию (определяется по смене PPID Claude Code процесса).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTEXT_FILE="$REPO_ROOT/.claude/session-context/current.md"
LOADED_FILE="$REPO_ROOT/.claude/session-context/.loaded-ts"

# ─── Защита от повторного инжекта в той же сессии ─────────────────────────────
# PPID не работает на Windows bash (всегда 1), используем time-based expiry (8 часов)
SESSION_TTL=28800  # 8 hours in seconds
if [ -f "$LOADED_FILE" ]; then
    LOADED_AT=$(cat "$LOADED_FILE")
    NOW=$(date +%s)
    AGE=$((NOW - LOADED_AT))
    if [ "$AGE" -lt "$SESSION_TTL" ]; then
        exit 0
    fi
fi

# Новая сессия — фиксируем
date +%s > "$LOADED_FILE"

CURRENT_BRANCH=$(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD 2>/dev/null)

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Нет сохранённого контекста — только git log как fallback ─────────────────
if [ ! -f "$CONTEXT_FILE" ]; then
    echo "📋 Сохранённого контекста нет. Ветка: \`${CURRENT_BRANCH}\`"
    echo ""
    echo "📊 Последние 5 коммитов:"
    cd "$REPO_ROOT" && git log -5 --format="%h %s (%ad)" --date=short 2>/dev/null
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 0
fi

# ─── Есть сохранённый контекст ────────────────────────────────────────────────
echo "📋 КОНТЕКСТ ПРЕДЫДУЩЕЙ СЕССИИ"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
cat "$CONTEXT_FILE"
echo ""

# Читаем сохранённые branch и commit hash
SAVED_HASH=$(grep -m1 "^Commit:" "$CONTEXT_FILE" | awk '{print $2}' | tr -d '`')
SAVED_BRANCH=$(grep -m1 "^Branch:" "$CONTEXT_FILE" | awk '{print $2}' | tr -d '`')

# ─── Коммиты с момента сохранения ─────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ -n "$SAVED_HASH" ]; then
    COMMITS_SINCE=$(cd "$REPO_ROOT" && git log --oneline --format="%h %s (%ad)" --date=short "${SAVED_HASH}..HEAD" 2>/dev/null)
    if [ -n "$COMMITS_SINCE" ]; then
        echo "📊 Коммиты с последнего сохранения:"
        echo "$COMMITS_SINCE"
    else
        echo "📊 Новых коммитов с последнего сохранения нет."
        echo "   Последние 3 коммита:"
        cd "$REPO_ROOT" && git log -3 --format="   %h %s (%ad)" --date=short 2>/dev/null
    fi
else
    echo "📊 Последние 3 коммита:"
    cd "$REPO_ROOT" && git log -3 --format="   %h %s (%ad)" --date=short 2>/dev/null
fi
echo ""

# ─── Предупреждение о смене ветки ─────────────────────────────────────────────
if [ -n "$SAVED_BRANCH" ] && [ -n "$CURRENT_BRANCH" ] && [ "$SAVED_BRANCH" != "$CURRENT_BRANCH" ]; then
    echo "⚠️  ВЕТКА СМЕНИЛАСЬ: сохранено на \`$SAVED_BRANCH\`, сейчас \`$CURRENT_BRANCH\`"
    echo "   Последние 3 коммита текущей ветки:"
    cd "$REPO_ROOT" && git log -3 --format="   %h %s (%ad)" --date=short 2>/dev/null
    echo ""
    echo "❓ Уточни у пользователя: это плановый переход или случайная смена ветки?"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
