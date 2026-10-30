"""Промпты SAM несут обязательные правила (gateway.prompts): основной, голосовой, ведущего."""
import re

import pytest

from gateway.prompts import SYSTEM_PROMPT
from gateway.prompts.identity import FEMALE, MALE, render
from gateway.prompts.lead import lead_prompt
from gateway.prompts.system import CHARACTER, HEARD, HEARING, SYSTEM_PROMPT_TEMPLATE, TIME_RULES, TONE
from gateway.prompts.voice import (
    ONE_SAM,
    VOICE_PROMPT_BASE,
    VOICE_PROMPT_TEMPLATE,
    voice_prompt_template,
)

CLOCK_TIME = re.compile(r"\d{1,2}:\d{2}")   # пример «14:11» быстрая модель называла как текущее время (SAM-171)


def _has(text: str, rule) -> bool:
    return bool(rule.search(text)) if isinstance(rule, re.Pattern) else rule in text


@pytest.mark.parametrize("prompt, required, forbidden", [
    (SYSTEM_PROMPT, [HEARD, "spawn_agent", "УСТНАЯ РЕЧЬ", HEARING, TIME_RULES, "делаешь сама"],
     ["noop", "МИНИМУМ 1 TOOL", "ОРКЕСТРАТОР", "<voice", "Видно?"]),
    (SYSTEM_PROMPT.lower(), ["русск"], []),
    (render(SYSTEM_PROMPT_TEMPLATE, MALE), ["говоришь правду", "магическим компаньоном можешь предложить", "позицию",
                                            "довод"], ["магическая сущность"]),
    (render(VOICE_PROMPT_TEMPLATE, MALE),
     ["УСТНАЯ РЕЧЬ", "Без путей", "разделить на", HEARING, TIME_RULES], []),
    (render(VOICE_PROMPT_TEMPLATE, FEMALE), [HEARING, TIME_RULES], []),
    (VOICE_PROMPT_TEMPLATE, [], ["повелитель", CLOCK_TIME]),
    (VOICE_PROMPT_BASE, [ONE_SAM], [CLOCK_TIME]),
    (ONE_SAM, ["я только голос", "посмотришь, что можно сделать"], []),
    (HEARING, ["переспроси", "бред", "не по подписи голоса"], []),
    (TIME_RULES, ["не додумывай"], []),
    (lead_prompt(MALE), [*(render(s, MALE) for s in (CHARACTER, TONE, HEARING, TIME_RULES)),
                         "УСТНАЯ РЕЧЬ"], ["spawn_agent", "ГОЛОСОВОЙ ХОД"]),
    (voice_prompt_template(), [], ["родной"]),
], ids=["main", "main_russian", "main_entity", "voice_male", "voice_female", "voice_template", "voice_base",
        "one_sam", "hearing", "time_rules", "lead", "voice_no_personal_address"])
def test_prompt_carries_required_rules(prompt, required, forbidden):
    assert [r for r in required if not _has(prompt, r)] == []
    assert [f for f in forbidden if _has(prompt, f)] == []


@pytest.mark.parametrize("rule", [
    re.compile(r"(?i)отказыва\w*[^.\n]*вред\w* другим"),
    re.compile(r"(?i)(наружу|от имени)[^.\n]*(«да»|соглас|разреш)"),
    re.compile(r"(?i)решил\w*[^.\n]*удал"),
], ids=["refuse_only_for_harm_to_others", "outward_only_after_yes", "decided_to_delete_deletes"])
def test_character_boundaries_reach_the_model(rule):
    """character 4, 25, 23: граница отказа, «наружу и от имени человека - после да», «решил удалить - удаляет».
    Модель получает их только текстом: промпт основной модели, промпт ведущего или скилл подачи по запросу."""
    from gateway import skills
    podacha = skills.load_skill_impl({"name": "podacha"}, skills.load_index(skills.PROJECT_SKILLS_DIR))
    assert any(_has(text, rule) for text in (SYSTEM_PROMPT, lead_prompt(MALE), podacha))


@pytest.mark.parametrize("rule", [
    re.compile(r"(?i)хорош\w*[^.\n]*«хорошо»[^.\n]*останов"),
    re.compile(r"(?i)делаю, как он решил[^.\n]*почему"),
    re.compile(r"(?i)споров[^.\n]*много[^.\n]*поломка"),
    re.compile(r"(?i)решает человек"),
    re.compile(r"(?i)сначала[^.\n]*(смущает|непонятн)[^.\n]*подробност[^.\n]*суть"),
    re.compile(r"(?i)одна-две фразы, одна главная мысль[^.\n]*почему[^.\n]*цена отказа"),
    re.compile(r"(?i)решение предлагаю после того, как он согласился, что проблема есть"),
    re.compile(r"(?i)общепринят\w* норм\w*[^.\n]*факт\w*[^.\n]*причин"),
    re.compile(r"(?i)норма никому не мешает[^.\n]*портрет"),
    re.compile(r"(?i)следующая ступень - только когда прошлая не сработала, и с паузой"),
    re.compile(r"(?i)твёрдо и уважительно[^\n]*позвать того, кто может повлиять"),
    re.compile(r"(?i)одной фразой признать чувство, через паузу продолжить"),
    re.compile(r"(?i)мелкое и отменяемое[^.\n]*сама[^.\n]*как отключить"),
    re.compile(r"(?i)тяжёлое[^.\n]*предлагаю[^.\n]*по просьбе"),
    re.compile(r"(?i)тон - по портрету"),
], ids=["praise_good_and_stop", "does_as_decided_with_reason", "many_disputes_is_my_breakage", "compass_human_decides",
        "answer_order", "explain_short_why_cost", "solutions_after_problem_seen", "common_norm_as_fact",
        "own_norm_to_portrait", "ladder_next_step_after_fail", "top_step_firm_then_call", "shouting_acknowledge",
        "small_reversible_self_with_off", "heavy_on_request", "tone_from_portrait"])
def test_character_rules_reach_the_model_through_the_podacha_skill(rule):
    """character P2 2, 3, 6, 7, 8, 11, 13, 16, 18, 20, 21, 24, 26, 28: правила подачи, которые видит человек, доходят
    до модели текстом скилла podacha (или промпта), а скилл есть в оглавлении основной модели и голоса нет."""
    from gateway import agent, skills
    podacha = skills.load_skill_impl({"name": "podacha"}, skills.load_index(skills.PROJECT_SKILLS_DIR))
    assert any(_has(text, rule) for text in (SYSTEM_PROMPT, lead_prompt(MALE), podacha))
    assert "podacha" in agent._with_skills(SYSTEM_PROMPT)


@pytest.mark.parametrize("rule", [
    re.compile(r"(?i)существенный пробел[^.]*прикидк[^.]*развилк"),
    re.compile(r"(?i)мелкий\s+пробел[^.]*оцениваю сама и называю"),
    re.compile(r"(?i)с учётом его ответов"),
    re.compile(r"(?i)прогноз - если он меняет решение, с тем, насколько он надёжен"),
    re.compile(r"(?i)не чувствует важности - образ последствий"),
    re.compile(r"(?i)забыл, зачем правило, - одной фразой напомнить причину"),
    re.compile(r"(?i)решение личное и время есть - вопросы[^\n]*кризис или срочный факт - прямо, с доводом"),
    re.compile(r"(?i)ставка или риск - попросить доводы[^\n]*азартной игрой[^\n]*бэктест"),
    re.compile(r"(?i)сделал - лестница сбрасывается"),
    re.compile(r"(?i)довод в крике слышу и пересматриваю своё"),
    re.compile(r"(?i)заботу показываю делом: возвращаюсь к вопросу, помню итог и довожу"),
], ids=["fork_estimate", "small_gap_named", "assess_with_answers", "forecast_when_it_matters", "picture_consequences",
        "rule_reason_in_one_phrase", "questions_or_straight", "bet_needs_reasons", "done_resets_ladder",
        "argument_in_a_shout", "care_by_deed"])
def test_delivery_details_reach_the_model_through_the_podacha_skill(rule):
    """character P3 9, 10, 12, 14, 15, 17, 19, 22, 27: детали подачи доходят до модели текстом скилла podacha."""
    from gateway import skills
    podacha = skills.load_skill_impl({"name": "podacha"}, skills.load_index(skills.PROJECT_SKILLS_DIR))
    assert any(_has(text, rule) for text in (SYSTEM_PROMPT, lead_prompt(MALE), podacha))
