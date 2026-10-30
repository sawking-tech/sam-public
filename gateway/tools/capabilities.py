"""list_capabilities — реальный enum инструментов SAM (главный агент + worker).

Запрещает галлюцинации типа "я могу открыть консоль" без реальной
проверки. Перед таким ответом агент должен вызвать этот tool.
"""
from __future__ import annotations

from typing import Any


def list_capabilities_impl(
    params: dict,
    main_tools: dict,        # TOOL_REGISTRY главного агента
    worker_tools: list[str], # WORKER_TOOL_NAMES (тулы доступные внутри create_task)
    mcp_summary_fn=None,     # callable(): str — кратко про MCP подключённые
) -> str:
    """Список доступных навыков с описаниями. Группируется по уровню:
    main / worker / MCP-внешние."""

    lines = []
    lines.append("МОИ ИНСТРУМЕНТЫ (главный агент — прямые вызовы):")
    for name, tool_cls in sorted(main_tools.items()):
        try:
            inst = tool_cls()
            desc = (inst.description or "").split(".")[0].strip()
            lines.append(f"  - {name}: {desc}")
        except Exception:
            lines.append(f"  - {name}")

    lines.append("")
    lines.append("ВНУТРИ create_task воркер может (а главный — нет, только через таску):")
    for name in worker_tools:
        lines.append(f"  - {name}")

    if mcp_summary_fn:
        try:
            mcp_info = mcp_summary_fn()
            if mcp_info:
                lines.append("")
                lines.append("MCP инструменты на устройствах (доступны через mcp_call внутри create_task):")
                lines.append(mcp_info)
        except Exception:
            pass

    return "\n".join(lines)


# Возможности для быстрой модели группами (ADR-027, блок 3): что можно обещать, без описаний инструментов.
# Имена берутся из реестра главного агента, группа - по этой карте; нового инструмента в карте нет - группа «прочее»
CAPABILITIES_HEADER = "Что умеет SAM (делаешь не ты, а основная модель следом; обещай только это):"
CAPABILITY_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("память: запомнить, поправить, найти, забыть", frozenset({
        "remember", "update_memory", "list_memories", "forget", "forget_topic", "compress_session",
        "recall_history"})),
    ("дом и устройства: что включено, каталог, разбудить компьютер", frozenset({
        "get_devices", "get_catalog", "set_household_member", "wake_device"})),
    ("приложения: открыть, спросить у приложения", frozenset({"app_list", "app_call"})),
    ("агенты: поиск в интернете, действия на компьютерах, код, музыка", frozenset({
        "spawn_agent", "list_agents", "cancel_agent", "send_to_agent", "grant_steps", "sandbox_exec"})),
    ("тихий режим", frozenset({"quiet"})),
    ("настроить себя: характер, настройки, навыки, модель", frozenset({
        "soul_show", "soul_edit", "soul_rollback", "tune_self", "get_self_config", "set_env", "get_env", "list_env",
        "restart_component", "switch_main_model", "sync_skills", "load_skill", "list_capabilities"})),
)
OTHER_GROUP = "прочее"


def capability_groups(main_tools: dict, groups=CAPABILITY_GROUPS) -> str:
    """Возможности SAM группами для быстрой модели: неизменны после старта, стоят в статике её промпта."""
    names = set(main_tools)
    if not names:
        return ""
    lines = [CAPABILITIES_HEADER]
    for label, members in groups:
        present = sorted(names & members)
        if present:
            lines.append(f"- {label} ({', '.join(present)})")
    other = sorted(names - set().union(*(m for _, m in groups)))
    if other:
        lines.append(f"- {OTHER_GROUP} ({', '.join(other)})")
    return "\n".join(lines)
