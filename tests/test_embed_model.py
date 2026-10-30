"""Модель эмбеддингов грузится из кэша без похода в сеть; в сеть - только когда модели в кэше нет."""
from gateway.embed_model import load_sentence_model


class _Factory:
    def __init__(self, cached):
        self.cached, self.calls = cached, []

    def __call__(self, name, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("local_files_only") and not self.cached:
            raise OSError("нет в кэше")
        return f"model:{name}"


def test_cached_model_loads_without_the_network():
    factory = _Factory(cached=True)
    assert load_sentence_model("m", factory) == "model:m"
    assert factory.calls == [{"local_files_only": True}]


def test_model_missing_from_cache_is_downloaded_once():
    factory = _Factory(cached=False)
    assert load_sentence_model("m", factory) == "model:m"
    assert factory.calls == [{"local_files_only": True}, {}]


def test_transformers_logging_is_restored_after_loading():
    from transformers.utils import logging as tf_logging
    before = tf_logging.get_verbosity()
    load_sentence_model("m", _Factory(cached=True))
    assert tf_logging.get_verbosity() == before
