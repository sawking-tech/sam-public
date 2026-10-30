"""Tools пакет — реализации tool-вызовов агента (SAM-40).

Каждый модуль группирует tools по домену:
  memory.py   — remember, list_memories, update_memory, forget
  catalog.py  — get_devices, get_catalog, wake_device
  tasks.py    — get_tasks, create_task, send_to_task, report_status
  web.py      — search_web
  mcp.py      — mcp_call
  claude.py   — claude_delegate

Каждый tool — pure function которая принимает DI зависимости (repos,
rag_store, и т.д.). Tool классы (BaseTool) — тонкие обёртки которые
делегируют impl. Это позволяет тестировать tool логику без агент-loop'а.
"""
