"""Разбор вызовов инструментов из текста (мета-формат) по реестру, переданному параметром."""
import pytest

from gateway.tool_text_parse import parse_channel_format

REGISTRY = {
    "report_status": [{"name": "text", "type": "string"}],
    "mcp_call": [{"name": "device"}, {"name": "tool"}, {"name": "args"}],
    "radar_where": {"type": "object", "properties": {"person": {"type": "string"}}},
}


@pytest.mark.parametrize("text, calls", [
    ('<|channel|>commentary to=functions.radar_where <|constrain|>json<|message|>{"person": "A"}<|call|>',
     [("radar_where", {"person": "A"})]),
    ("<|channel|>final<|message|>Привет<|end|>", []),                       # финальный канал Harmony - не вызов
    ('<tool_call>{"name": "radar_where", "arguments": {"person": "A"}}</tool_call>',
     [("radar_where", {"person": "A"})]),
    ('[report_status("иду")]', [("report_status", {"text": "иду"})]),         # строка - первому параметру
    ('[delete_all("x")]', []),                                               # нет в реестре
    ('[tool_call: mcp_call(device="pc", tool="screenshot")]', [("mcp_call", {"device": "pc", "tool": "screenshot"})]),
    ('mcp_call("pc", "run_command", {"command": "dir"})',                     # позиционные - по реестру
     [("mcp_call", {"device": "pc", "tool": "run_command", "args": {"command": "dir"}})]),
    ('[radar_where("A")]', [("radar_where", {"input": "A"})]),                # у JSON-схемы MCP строка уходит в input
    ("Ирина в зале, на диване.", []),
], ids=["harmony", "harmony_final", "tool_call_tag", "bracket_string", "bracket_unknown", "tool_call_kwargs",
        "bare_positionals", "json_schema_input", "plain_text"])
def test_text_calls_are_parsed_by_registry(text, calls):
    assert parse_channel_format(text, REGISTRY) == calls


GLM_CALL = ('Сейчас посмотрю.<tool_call>mcp_call<arg_key>device</arg_key><arg_value>pc</arg_value>'
            '<arg_key>tool</arg_key><arg_value>run_command</arg_value>'
            '<arg_key>args</arg_key><arg_value>{"command": "dir"}</arg_value></tool_call>')
# SAM-217: так glm-5.3-flash писала вызов в ответ 15 окт (16:23:10, voice) - метка и JSON без тегов
FLASH_CALL = '[tool_call] {"name": "mcp_call", "arguments": {"device": "pc", "tool": "run_command", "args": {"command": "dir"}}}'
GEMMA_CALL = ('<|tool_call>call:mcp_call{device:<|"|>pc<|"|>,tool:<|"|>run_command<|"|>,'
              'args:{command:<|"|>dir C:\\\\<|"|>,wait:true,lines:20}}<tool_call|>')


@pytest.mark.parametrize("text, calls", [
    (GLM_CALL, [("mcp_call", {"device": "pc", "tool": "run_command", "args": {"command": "dir"}})]),
    ("<tool_call>radar_where<arg_key>person</arg_key><arg_value>'Ирина'</arg_value></tool_call>",
     [("radar_where", {"person": "Ирина"})]),                                # строка не JSON - без кавычек
    (GEMMA_CALL, [("mcp_call", {"device": "pc", "tool": "run_command",
                                "args": {"command": "dir C:\\", "wait": True, "lines": 20}})]),
    ('<|tool_call>call:radar_where{person:<|"|>Ирина, мама<|"|>}<tool_call|>',
     [("radar_where", {"person": "Ирина, мама"})]),                          # запятая внутри строки
    (FLASH_CALL, [("mcp_call", {"device": "pc", "tool": "run_command", "args": {"command": "dir"}})]),
    ('[tool_call] {"name": "delete_all", "arguments": {}}', []),             # нет в реестре
    ('[tool_call] {"name": "report_status", "arguments": "{\\"text\\": \\"иду\\"}"}', [("report_status", {"text": "иду"})]),
], ids=["glm4", "glm4_quoted_string", "gemma4_nested", "gemma4_comma_in_string", "flash_bracket_json",
        "flash_unknown", "flash_string_arguments"])
def test_glm4_and_gemma4_calls_are_parsed(text, calls):
    """agent-loop 12, 13: форматы вызовов GLM-4 (основная модель стенда) и Gemma 4 разбираются в имя и аргументы."""
    assert parse_channel_format(text, REGISTRY) == calls


@pytest.mark.parametrize("text", [GLM_CALL, GEMMA_CALL, FLASH_CALL], ids=["glm4", "gemma4", "flash"])
async def test_glm4_and_gemma4_calls_run_in_the_loop_and_are_not_said(text):
    """agent-loop 12, 13: вызов текстом GLM-4 и Gemma 4 исполняется циклом, а не уходит человеку текстом."""
    from gateway import agent_loop
    from tests.fakes_agents import FakeLLMClient, _resp, llm_cfg, loop_deps
    ran = []

    async def mcp_call(args):
        ran.append(args)
        return "файлы: a.txt"
    deps = loop_deps(llm_client=FakeLLMClient([_resp(content=text), _resp(content="Там один файл.")]),
                     tool_impl={"mcp_call": mcp_call})
    answer, _ = await agent_loop.run_agent_loop(deps, [{"role": "user", "content": "что в папке"}], llm_cfg(),
                                                ["mcp_call"])
    assert ran and ran[0]["tool"] == "run_command" and answer == "Там один файл."
