#!/bin/bash
# Установка MCP серверов для SAM на каждый ПК
# Требует: Node.js 18+, npm
# Запускать: bash mcp/install.sh

set -e

echo "=== Установка MCP серверов для SAM ==="

# mcp-proxy — HTTP/SSE обёртка для stdio MCP серверов
npm install -g mcp-proxy

# Базовые MCP серверы
npm install -g @modelcontextprotocol/server-filesystem
npm install -g @modelcontextprotocol/server-playwright

# Windows Control MCP (только Windows)
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OS" == "Windows_NT" ]]; then
    npm install -g @modelcontextprotocol/server-windows-control
    echo "Windows Control MCP установлен"
fi

echo ""
echo "=== Готово ==="
echo "Следующий шаг:"
echo "  Проверка: bash mcp/check-mcp.sh"
