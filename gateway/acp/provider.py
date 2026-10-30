"""ACP-агент как поставщик хода для llm_client (ADR-022).

Шлюз собирает ход как для любой модели (история, память, скиллы, судьи, фолбэк) и зовёт поставщика; этот поставщик
отдаёт ход агенту текстом и возвращает ответ в виде ответа OpenAI: поток - чанками, без потока - целым сообщением.
Инструменты агент вызывает сам через MCP SAM, поэтому вызовов инструментов в ответе нет и цикл шлюза кончается на
первом шаге. Ход инструмента агента - пустым чанком (срок молчания поставщика считается заново) и в on_piece.
Ошибка агента - LLMProviderError: llm_client ставит поставщика в паузу и берёт следующего по роутеру.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

from ..llm_client import LLMProviderError
from .agent import AcpAgent, AcpError
from .config import SOURCE
from .prompt import TEXT, THOUGHT, TOOL, Piece, prompt_text

# Агент не поднялся или умер: как 503 у HTTP-поставщика - пауза и замена (provider_transform.classify_error)
UNAVAILABLE_STATUS = 503
STOP = "stop"


def _chunk(content: str | None = None, reasoning: str | None = None, finish: str | None = None) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None)


_KEEPALIVE = SimpleNamespace(choices=[], usage=None)


def _response(text: str, reasoning: str) -> SimpleNamespace:
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None,
                              reasoning_content=reasoning or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=STOP)], usage=None)


class AcpProvider:
    def __init__(self, agent: AcpAgent, on_piece: Callable[[Piece], None] = lambda piece: None) -> None:
        self._agent = agent
        self._on_piece = on_piece

    async def call(self, llm_config: dict, *, messages: list[dict], stream: bool = False, **ignored: Any) -> Any:
        """Сигнатура вызова поставщика llm_client: (конфиг, **параметры запроса); tools и прочее агенту не нужны."""
        text = prompt_text(messages)
        if stream:
            return self._stream(text)
        answer, reasoning = [], []
        async for chunk in self._stream(text):
            for choice in chunk.choices:
                answer.append(choice.delta.content or "")
                reasoning.append(choice.delta.reasoning_content or "")
        return _response("".join(answer), "".join(reasoning))

    async def _stream(self, text: str) -> AsyncIterator[SimpleNamespace]:
        try:
            async for piece in self._agent.turn(text):
                self._notify(piece)
                if piece.kind == TEXT and piece.text:
                    yield _chunk(content=piece.text)
                elif piece.kind == THOUGHT and piece.text:
                    yield _chunk(reasoning=piece.text)
                elif piece.kind == TOOL:
                    yield _KEEPALIVE
        except AcpError as e:
            raise LLMProviderError(SOURCE, UNAVAILABLE_STATUS, str(e)) from e
        yield _chunk(finish=STOP)

    def _notify(self, piece: Piece) -> None:
        try:
            self._on_piece(piece)
        except Exception as e:
            print(f"[ACP] on_piece: {e}")
