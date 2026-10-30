"""Сеть песочницы по запросу (SAM-83, ADR-016): интернет есть, дом и хост закрыты.

Запуск песочницы по умолчанию без сети. Когда задаче нужна сеть (поставить пакет, клонировать репозиторий, скачать
файл), контейнер встаёт в свою сеть `SAM_SANDBOX_NET` с подсетью `SAM_SANDBOX_SUBNET`, а в цепочке `DOCKER-USER` -
официальном месте Docker для своих правил - стоит запрет из этой подсети во все частные и служебные адреса: роутер,
ПК и телефоны дома, хост Docker, шлюз SAM, MCP, другие сети Docker. Правила ставит служебный образ `sam-netguard`
(iptables) привилегированным в сети хоста Docker - одинаково на Docker Desktop и на Ubuntu. Правила теряются при
перезапуске Docker, поэтому ставятся перед каждым запуском с сетью; не встали - сети нет.

Решения (подсеть, диапазоны, скрипт правил, команды docker) отделены от запуска процесса: `run` приходит параметром.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Callable

NET_ENV, DEFAULT_NET = "SAM_SANDBOX_NET", "sam-sandbox-net"
SUBNET_ENV, DEFAULT_SUBNET = "SAM_SANDBOX_SUBNET", "10.232.0.0/24"
GUARD_IMAGE_ENV, DEFAULT_GUARD_IMAGE = "SAM_SANDBOX_GUARD_IMAGE", "sam-netguard:1"
GUARD_BUILD = f"docker build -f docker/Dockerfile.netguard -t {DEFAULT_GUARD_IMAGE} docker"
CHAIN = "DOCKER-USER"
# docker network inspect -f: включён ли IPv6 и все подсети существующей сети («true 10.232.0.0/24 fd00::/64 »)
NET_FORMAT = "{{.EnableIPv6}} {{range .IPAM.Config}}{{.Subnet}} {{end}}"
# Куда сеть песочницы не ходит никогда: частные сети (RFC 1918) - дом и сети Docker, CGNAT, link-local, loopback
CLOSED = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8")
NONE, INTERNET = "none", "internet"
MODES = (NONE, INTERNET)

Run = Callable[[list[str]], "object"]       # возвращает объект с code, out, err (apps.DockerResult)


def net_name() -> str:
    return os.getenv(NET_ENV) or DEFAULT_NET


def subnet() -> str:
    value = os.getenv(SUBNET_ENV) or DEFAULT_SUBNET
    return str(ipaddress.ip_network(value))     # ошибка в настройке - ValueError сразу, а не дыра в правилах


def guard_script(sub: str) -> str:
    """Скрипт для sh в контейнере netguard: для каждого закрытого диапазона правило DROP, если его ещё нет
    (`-C` проверяет), - повторный вызов ничего не дублирует; в конце печатает число правил песочницы."""
    lines = [f"iptables -C {CHAIN} -s {sub} -d {d} -j DROP 2>/dev/null || iptables -I {CHAIN} -s {sub} -d {d} -j DROP"
             for d in CLOSED]
    lines.append(f"iptables -S {CHAIN} | grep -c -- '-s {sub}'")
    return " && ".join(lines)


def guard_argv(docker: str, sub: str, image: str) -> list[str]:
    return [docker, "run", "--rm", "--privileged", "--net=host", "--pull=never", image, "sh", "-c", guard_script(sub)]


def create_argv(docker: str, sub: str, name: str) -> list[str]:
    """Сеть песочницы только IPv4: правила CLOSED - IPv4, адрес IPv6 обошёл бы их."""
    return [docker, "network", "create", "--ipv6=false", "--subnet", sub, name]


def network_mismatch(name: str, found: str, sub: str) -> str | None:
    """Существующая сеть годится, только если IPv6 выключен и среди её подсетей есть `sub`: правила закрывают IPv4
    подсеть `sub`, контейнер в другой подсети или с адресом IPv6 их обошёл бы. Отказ - одной строкой."""
    fix = f"сеть не дана; удалите сеть - docker network rm {name}"
    ipv6, *subnets = found.split() or [""]
    if not ipv6:
        return f"Docker не описал сеть песочницы {name} (ни IPv6, ни подсетей): {fix}"
    if ipv6.lower() == "true":
        return f"В сети песочницы {name} включён IPv6, а правила только IPv4: {fix}"
    if not any(_same_net(s, sub) for s in subnets):
        return (f"Сеть песочницы {name} в подсетях {' '.join(subnets) or '?'}, а {SUBNET_ENV} - {sub}: правила её "
                f"не закроют, {fix}")
    return None


def _same_net(value: str, sub: str) -> bool:
    try:
        return ipaddress.ip_network(value) == ipaddress.ip_network(sub)
    except ValueError:
        return False


def ensure(run: Run, docker: str = "docker") -> str | None:
    """Сеть песочницы и правила файрвола на месте. Ошибка - одной строкой, тогда сети у задачи нет; None - готово."""
    sub, name = subnet(), net_name()
    inspect = [docker, "network", "inspect", "-f", NET_FORMAT, name]
    found = run(inspect)
    if found.code != 0:
        created = run(create_argv(docker, sub, name))
        if created.code == 0:
            found = None
        else:   # два запуска с сетью одновременно: сеть мог создать соседний - перечитать и сверить так же
            found = run(inspect)
            if found.code != 0:
                return f"Сеть песочницы {name} не создана: {_line(created.err)}"
    if found is not None and (error := network_mismatch(name, found.out or "", sub)):
        return error
    image = os.getenv(GUARD_IMAGE_ENV) or DEFAULT_GUARD_IMAGE
    res = run(guard_argv(docker, sub, image))
    if res.code != 0:
        hint = f"; соберите образ - {GUARD_BUILD}" if "no such image" in (res.err or "").lower() else ""
        return f"Файрвол песочницы не встал, сеть не дана: {_line(res.err)}{hint}"
    try:
        if int((res.out or "0").strip().splitlines()[-1]) < len(CLOSED):
            return "Файрвол песочницы встал не полностью, сеть не дана"
    except ValueError:
        return "Файрвол песочницы не подтвердил правила, сеть не дана"
    return None


def _line(text: str) -> str:
    lines = [s for s in (text or "").strip().splitlines() if s.strip()]
    return lines[-1] if lines else "без вывода"
