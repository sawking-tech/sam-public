"""Cortex - рассуждение SAM (docs/brain-map.md): подмодули loop, workers, llm, acp.

acp - свой агент SAM за протоколом ACP (ADR-022), подпроцесс шлюза. loop, workers и llm пока живут в gateway
(agent_loop, worker, llm_client, llm_router) и переезжают сюда, когда их трогают.
"""
