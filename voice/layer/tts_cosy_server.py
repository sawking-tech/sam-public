"""CosyVoice3 (Fun-CosyVoice3-0.5B, ускоренный) как Wyoming-сервер: say_out ходит к нему так же, как к Silero.

  E:\\tts-lab-cosy\\venv\\Scripts\\python.exe -m voice.layer.tts_cosy_server [--port 10203]

Запускается в venv лаборатории (torch, cosyvoice) из корня SAM: в окружении SAM их нет. Ускорение взято из лаборатории
E:\\tts-lab-cosy\\graph (progress.md, run_final.py): шаг LLM одним CUDA graph, голос закэширован один раз,
первый кусок через 25 токенов, графы flow под длины кусков, flow в fp16 - первый звук ~0,95 с на 3090 Ti.
Графы захватываются при старте в главном потоке, до первого запроса; во время синтеза захват запрещён.
Голоса - из реестра голосов (voice_registry): сервер держит слепки всех голосов с разделом
cosy и берёт голос по полю voice запроса, без перезапуска; голоса нет на cosy - голос cosy по умолчанию.
Голос, добавленный в реестр после старта, ставится при первом запросе. Слепок снимается с образца один раз и
лежит на диске. Графы flow сняты под длину образца одного голоса (COSY_VOICE, по умолчанию
голос cosy из defaults): у остальных flow идёт без графа, первый звук медленнее (вывод, не замерено).
Синтез потоковый: куски уходят клиенту по мере готовности.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

from .stress import plain
from .voice_bank import VoiceBank
from .voice_registry import Live, Registry, Voice
from .wyoming_server import serve
from gateway.env import env_bool, env_int

NAME = "cosy"
LABEL = f"[синтез] {NAME}:"
PORT = env_int("COSY_PORT", 10203)
# Лаборатория с ускоренными модулями (common, graph_llm, graph_flow); common сам ставит пути к repo и кэшам на E
LAB = Path(os.getenv("COSY_LAB", r"E:\tts-lab-cosy\graph"))
# Голос, под длину образца которого снимаются графы flow; пусто - голос cosy по умолчанию из реестра
GRAPH_VOICE = os.getenv("COSY_VOICE", "") or None
# fp16 flow: первый кусок 0,36 с против 0,55 у tf32, отличие мел-спектра 0,215 против 0,114 (progress.md 21:15)
FP16 = env_bool("COSY_FP16", True)
# Upstream растит hop 25 -> 50 -> 100 и не сбрасывает между вызовами: без сброса первый кусок ждёт 103 токена
FIRST_HOP = 25
INSTRUCT = "You are a helpful assistant.<|endofprompt|>"  # префикс подсказки CosyVoice3, как в common.PROMPT
# Подсказка манеры: ставится в префикс подсказки слепка голоса (with_mood). Пусто - обычный zero-shot по слепку
MOOD = os.getenv("COSY_MOOD", "Speak cheerfully and energetically, at a brisk even pace, with crisp short word endings.")
WARMUP = ["Проверка связи.", "Это вторая фраза прогрева, чуть длиннее первой."]


def log(line: str) -> None:
    print(line, flush=True)


def lab_common():
    """common.py лаборатории под своим именем: имя common слишком общее, чтобы класть его в sys.modules."""
    spec = importlib.util.spec_from_file_location("cosy_lab_common", LAB / "common.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def installer(cv, torch):
    """Как поставить голос в модель под его именем: слепок с диска, если есть; иначе снять с образца и сохранить."""
    def install(voice: Voice) -> str:
        path = voice.imprint
        if path and path.exists():
            cv.frontend.spk2info[voice.name] = torch.load(path, map_location="cuda" if torch.cuda.is_available() else "cpu")
            return f"из {path.name}"
        if not (voice.ref and voice.text):
            raise ValueError(f"у голоса {voice.name} нет образца и расшифровки в реестре (cosy: ref, text)")
        prompt = INSTRUCT + voice.text.read_text(encoding="utf-8").strip()
        cv.add_zero_shot_spk(prompt, str(voice.ref), voice.name)
        if not path:
            return f"снят с образца {voice.ref.name}, слепок не сохранён: в реестре нет imprint"
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(cv.frontend.spk2info[voice.name], path)
        return f"снят с образца {voice.ref.name} и сохранён в {path.name}"
    return install


def load(registry: Registry) -> tuple[object, VoiceBank]:
    sys.path.insert(0, str(LAB))
    common = lab_common()
    import torch
    import graph_flow
    import graph_llm
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    cv = common.load()
    bank = VoiceBank(installer(cv, torch), log, LABEL)
    for name in registry.names(NAME):
        bank.ensure(registry.choose(name, NAME))
    graphed = bank.use(registry.choose(GRAPH_VOICE, NAME))
    graph_llm.install(cv)
    graph_flow.install(cv, graphed, fp16=FP16)
    log(f"{LABEL} графы flow сняты под голос {graphed}")
    common.fast_poll()
    return cv, bank


def stream(cv, text: str, spk: str):
    """Куски одной реплики по мере готовности; брошенный поток дорабатывается, чтобы LLM не пересёкся со следующим."""
    cv.model.token_hop_len = FIRST_HOP
    inner = (with_mood(cv, text, spk) if MOOD else
             cv.inference_zero_shot(text, "", "", zero_shot_spk_id=spk, stream=True, text_frontend=False))
    try:
        for out in inner:
            yield out["tts_speech"].squeeze(0).numpy(), cv.sample_rate
    finally:
        for _ in inner:
            pass


def with_mood(cv, text: str, spk: str):
    """Zero-shot по слепку голоса с манерой в префиксе подсказки. Речевые токены образца и его расшифровка
    остаются: без них (чистый instruct2) речь тянет окончания (на слух, 23 окт). Префикс INSTRUCT в
    prompt_text слепка заменяется на префикс с MOOD."""
    import torch
    plain_len = cv.frontend._extract_text_token(INSTRUCT)[0].shape[1]
    mood = cv.frontend._extract_text_token(f"You are a helpful assistant. {MOOD}<|endofprompt|>")[0]
    for part in cv.frontend.text_normalize(text, split=True, text_frontend=False):
        model_input = cv.frontend.frontend_zero_shot(part, "", "", cv.sample_rate, spk)
        prompt = torch.cat([mood.to(model_input["prompt_text"].device), model_input["prompt_text"][:, plain_len:]], dim=1)
        model_input["prompt_text"] = prompt
        model_input["prompt_text_len"] = torch.tensor([prompt.shape[1]], dtype=torch.int32)
        yield from cv.model.tts(**model_input, stream=True, speed=1.0)


def warm(cv, spk: str) -> None:
    for text in WARMUP:  # первый проход по каждому пути медленный: прогреть до первого запроса
        for _ in stream(cv, text, spk):
            pass


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=PORT)
    a = p.parse_args()
    live = Live(label=LABEL)
    registry = live.get()
    cv, bank = load(registry)
    warm(cv, registry.choose(GRAPH_VOICE, NAME).name)

    def synth(text: str, voice: str | None):
        # Реестр перечитывается при правке файла: новый голос или образец - без перезапуска сервера
        spk = bank.use(live.get().choose(voice, NAME))
        # «+» - разметка ударений для Silero, CosyVoice прочитал бы её как символ
        return stream(cv, plain(text), spk)

    serve(NAME, a.port, synth, registry.names(NAME))


if __name__ == "__main__":
    main()
