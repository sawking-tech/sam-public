"""Базы стенда без пароля слушают только эту машину (docker/docker-compose.*.yml): иначе память SAM читает любое
устройство домашней сети."""
from pathlib import Path

import pytest
import yaml

DOCKER = Path(__file__).resolve().parent.parent / "docker"
COMPOSE_FILES = ("docker-compose.dev.yml", "docker-compose.server.yml")
CLOSED_SERVICES = ("mongodb", "qdrant")
LOOPBACK = "127.0.0.1:"


def _published(compose: str) -> dict[str, list[str]]:
    services = yaml.safe_load((DOCKER / compose).read_text(encoding="utf-8"))["services"]
    return {name: [str(p) for p in spec.get("ports", [])] for name, spec in services.items()}


@pytest.mark.parametrize("compose", COMPOSE_FILES)
def test_databases_are_published_on_loopback_only(compose):
    published = _published(compose)
    for service in CLOSED_SERVICES:
        assert published.get(service), f"{compose}: нет сервиса {service}"
        assert all(port.startswith(LOOPBACK) for port in published[service]), (compose, service, published[service])
