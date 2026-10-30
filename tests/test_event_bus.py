"""Шина событий (SAM-72, SAM-76, SAM-97): запись истории в Mongo и раздача монитору.

История - mongomock; монитор - настоящий /ws/monitor через TestClient без стенда (fakes_core.app_client)."""
import threading

import pytest
from starlette.websockets import WebSocketDisconnect

from gateway.event_bus import SUBSCRIBER_DROPPED, EventBus
from gateway.repos.session_repo import CHANNEL, TEXT_CHANNEL
from tests.fakes_core import app_client  # noqa: F401  фикстура

SUBSCRIBE_TIMEOUT_S = 10.0   # предел ожидания, не пауза: подписка на mongomock - миллисекунды
SCHEME = "┌──┐\n│ГW│──> монитор\n└──┘  <tag>"


class _DownDb:
    """Mongo, у которой упала запись."""

    class sessions:
        @staticmethod
        def insert_one(doc):
            raise RuntimeError("mongo down")


def _bus(db, listener=None):
    bus = EventBus()
    bus.set_db(db)
    if listener is not None:
        bus.set_history_listener(listener)
    return bus


def test_history_write_keeps_record_and_survives_failures(mongo_db, capsys):
    from gateway import main

    # Результат инструмента с linger (тело скилла) пишется целиком, обычный - нет
    bus = _bus(mongo_db)
    bus.emit("tool_result", {"name": "radar_where", "result": "кухня", "session": "s1"})
    bus.emit("tool_result", {"name": "load_skill", "result": "Скилл", "content": "Скилл radar целиком",
                             "linger": True, "session": "s1"})
    docs = list(mongo_db.sessions.find({"session_id": "s1"}))
    assert [d["role"] for d in docs] == ["tool"]
    assert docs[0]["content"] == "Скилл radar целиком" and docs[0]["linger"] and docs[0]["tool_name"] == "load_skill"

    # Текст без озвучки хранит канал text: монитор после F5 показывает его тем же каналом
    bus.emit("assistant_reply", {"content": SCHEME, "session": "voice", CHANNEL: TEXT_CHANNEL})
    note = mongo_db.sessions.find_one({"session_id": "voice"})
    assert note["content"] == SCHEME and note[CHANNEL] == TEXT_CHANNEL
    assert main._replay_marks(note)[CHANNEL] == TEXT_CHANNEL

    # Упал слушатель записи (постановка сжатия) - запись есть, строка в логе
    _bus(mongo_db, listener=lambda sid: 1 / 0).emit("user_message", {"content": "привет", "session": "s2"})
    assert mongo_db.sessions.count_documents({"session_id": "s2"}) == 1
    assert "history listener failed" in capsys.readouterr().out

    # Упала Mongo - строка в логе с сессией, а не тишина
    _bus(_DownDb()).emit("user_message", {"content": "привет", "session": "s3"})
    out = capsys.readouterr().out
    assert "mongo down" in out and "s3" in out


def test_slow_subscriber_is_dropped_with_marker():
    """Живой подписчик получает события; переполненный отписывается и получает маркер, чтобы монитор
    переподключился."""
    bus = EventBus()
    live = bus.subscribe()
    bus.emit("tick", {"n": 1})
    assert '"tick"' in live.get_nowait()

    for i in range(live.maxsize + 1):
        bus.emit("tick", {"n": i})
    assert live not in bus._subscribers
    assert live.get_nowait() == SUBSCRIBER_DROPPED


class _LeavingSocket:
    """Клиент монитора, который принял снимок и ушёл: uvicorn сообщает об уходе только через receive."""

    async def accept(self):
        pass

    async def send_text(self, text):
        pass

    async def receive_text(self):
        raise WebSocketDisconnect(code=1001)

    async def close(self, code=1000, reason=None):
        pass


def test_monitor_socket_frees_its_slot_when_client_leaves_in_silence(monkeypatch):
    """Закрытая вкладка монитора освобождает слот сразу, без события на шине: иначе ушедшие клиенты
    занимают все _MAX_WS_CONNECTIONS и новые получают 403."""
    import asyncio
    from gateway import main
    monkeypatch.setattr(main, "_build_mongo_snapshot", lambda *a, **k: main.MonitorPage([], None, 0))
    before = main._ws_count

    asyncio.run(asyncio.wait_for(main.monitor_ws(_LeavingSocket()), SUBSCRIBE_TIMEOUT_S))

    assert main._ws_count == before, "ушедший клиент держит слот монитора"


def test_monitor_socket_closes_when_dropped(app_client, monkeypatch):
    """WS /ws/monitor закрывается с 1013, когда шина отписала монитор."""
    from gateway import main
    client, _ = app_client
    subscribed = threading.Event()
    subscribe = main.event_bus.subscribe

    def _subscribe():
        # Подписка идёт после снимка истории в потоке сервера: событие раньше неё потеряется
        queue = subscribe()
        subscribed.set()
        return queue
    monkeypatch.setattr(main.event_bus, "subscribe", _subscribe)

    with client.websocket_connect("/ws/monitor") as ws:
        assert subscribed.wait(SUBSCRIBE_TIMEOUT_S), "монитор не подписался на шину"
        main.event_bus.emit("ping", {"n": 1})
        while '"ping"' not in ws.receive_text():
            pass
        main.event_bus._drop(main.event_bus._subscribers[-1])
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1013


def test_monitor_gets_the_last_page_of_all_sessions_on_connect(app_client, monkeypatch):
    """stand 24: после F5 монитор получает последнюю порцию (MONITOR_FIRST_PAGE) реплик всех сессий по времени и скрытые фразы (фон) за то же
    время - экран не пустой и не перемешан. История на mongomock, подключение - настоящий WS /ws/monitor."""
    import json
    from datetime import datetime, timedelta

    from gateway import main
    client, db = app_client
    monkeypatch.setattr(main, "_mongo_ok", True)
    t0 = datetime(2026, 10, 14, 12, 0)
    db.sessions.insert_many([{"session_id": "voice" if i % 2 else "workpc", "role": "user", "content": f"m{i}",
                              "timestamp": t0 + timedelta(seconds=i)} for i in range(310)])
    db.sessions.insert_many([
        {"session_id": "voice", "role": "user", "content": "старый фон", "ignore": "фон", "timestamp": t0},
        {"session_id": "voice", "role": "user", "content": "свежий фон", "ignore": "фон",
         "timestamp": t0 + timedelta(seconds=305, milliseconds=500)}])

    with client.websocket_connect("/ws/monitor") as ws:
        begin = json.loads(ws.receive_text())
        got = []
        while (ev := json.loads(ws.receive_text()))["type"] != main.SNAPSHOT_END:
            got.append(ev)
        end = ev

    # требование: последние реплики всех сессий по времени, фон своего времени - на месте, старый фон - нет
    assert begin == {"type": main.SNAPSHOT_BEGIN}                      # снимок в рамке: монитор показывает его разом
    assert end["oldest"] == got[0]["ts_iso"]
    assert end["full"] is True       # истории больше порции: после долгого обрыва монитор покажет возможную дыру
    texts = [e["content"] for e in got]
    assert texts[-1] == "m309" and texts.index("свежий фон") == texts.index("m305") + 1 and "старый фон" not in texts
    assert {e["session"] for e in got} == {"voice", "workpc"}
    first = int(texts[0][1:])

    # листают вверх: следующая порция - ровно предыдущие реплики, без пропуска и в том же порядке
    older = client.get("/sam/monitor/history",
                       params={"before": end["oldest"], "before_id": end["oldest_id"], "limit": 5}).json()
    assert [e["content"] for e in older["events"]] == [f"m{i}" for i in range(first - 5, first)]
    assert older["oldest"] == older["events"][0]["ts_iso"]
    assert client.get("/sam/monitor/history", params={"before": end["oldest"], "before_id": "x"}).status_code == 400
    assert client.get("/sam/monitor/history", params={"before": "вчера"}).status_code == 400


def test_monitor_history_keeps_records_of_one_millisecond_and_reads_one_session(app_client, monkeypatch):
    """Порция истории, упёршаяся в предел посреди записей одной миллисекунды (вызов и итог инструмента), их не
    теряет: следующая порция берёт границу снова. session - лента одного агента; запись без события (тело скилла)
    не сдвигает следующую порцию вперёд."""
    from datetime import datetime, timedelta

    from gateway import main
    client, db = app_client
    monkeypatch.setattr(main, "_mongo_ok", True)
    t0 = datetime(2026, 10, 14, 22, 2, 32)
    db.sessions.insert_many(
        [{"session_id": "task:b343f470", "role": "tool_call", "tool_name": f"t{i}", "tool_args": {}, "content": "",
          "msg_id": f"c{i}", "timestamp": t0 + timedelta(seconds=i // 3)} for i in range(9)]
        + [{"session_id": "task:b343f470", "role": "tool", "tool_name": "load_skill", "content": "тело",
            "msg_id": "skill", "timestamp": t0 - timedelta(seconds=1)},
           {"session_id": "homepc", "role": "user", "content": "чужая", "msg_id": "u", "timestamp": t0}])

    seen, cursor = [], {"before": (t0 + timedelta(seconds=10)).isoformat() + "Z"}
    for _ in range(10):
        page = client.get("/sam/monitor/history", params={**cursor, "limit": 2, "session": "task:b343f470"}).json()
        seen += [e["msg_id"] for e in page["events"]]
        if not page["oldest"]:
            break
        cursor = {"before": page["oldest"], "before_id": page["oldest_id"]}
    assert sorted(seen) == [f"c{i}" for i in range(9)]      # ни один вызов не потерян и не повторён, чужой нет


def test_monitor_lists_tasks_and_fresh_sessions(app_client, monkeypatch):
    """Панель задач после F5 берёт задачи из sam.tasks (события task_* в историю не пишутся); селектор сессии -
    свежие первыми, с видом каждой."""
    from datetime import datetime, timedelta

    from gateway import main
    client, db = app_client
    monkeypatch.setattr(main, "_mongo_ok", True)
    monkeypatch.setattr(main, "_db", db)
    t0 = datetime(2026, 10, 14, 22, 0)
    db.tasks.insert_many([
        {"id": "b343f470-3679-453d-842b-1495b6b3ae54", "name": "veo_presentation", "description": "презентация",
         "status": "done", "result": "ОТВЕТ: готово", "created_at": t0, "device": "homepc"},
        {"id": "6e1cfc46-0000-0000-0000-000000000000", "name": "veo_check", "description": "проверка",
         "status": "running", "created_at": t0 + timedelta(minutes=10)}])
    db.sessions.insert_many([
        {"session_id": "old", "role": "user", "content": "a", "timestamp": t0},
        {"session_id": "task:b343f470", "role": "tool_call", "content": "", "timestamp": t0 + timedelta(minutes=1)},
        {"session_id": "homepc", "role": "user", "content": "b", "timestamp": t0 + timedelta(minutes=2)}])

    tasks = client.get("/sam/tasks").json()
    assert [t["task_id"] for t in tasks] == ["6e1cfc46", "b343f470"]
    assert tasks[1]["name"] == "veo_presentation" and tasks[1]["status"] == "done"
    assert tasks[1]["result"] == "ОТВЕТ: готово" and tasks[1]["created_at"] == "2026-10-14T22:00:00Z"
    sessions = [(r["id"], r["kind"]) for r in client.get("/sam/sessions").json()]
    # свежие первыми, вид у каждой; устройство этой машины (SAM_LOCAL_DEVICE тестов) - канал человека и без истории
    assert sessions == [("homepc", "check"), ("task:b343f470", "agent"), ("old", "check"), ("workpc", "person")]


def test_monitor_page_cursor_is_the_last_visible_record_not_a_hidden_one(app_client, monkeypatch):
    """Порция упёрлась в предел видимых посреди одной миллисекунды, а скрытая запись той же миллисекунды с меньшим
    _id - не курсор: видимые между ними приходят следующей порцией, скрытая - один раз."""
    from datetime import datetime

    from bson import ObjectId

    from gateway import main
    client, db = app_client
    monkeypatch.setattr(main, "_mongo_ok", True)
    t0 = datetime(2026, 10, 14, 22, 2, 32)
    ids = sorted(ObjectId() for _ in range(5))
    db.sessions.insert_many(
        [{"_id": ids[0], "session_id": "voice", "role": "user", "content": "фон", "ignore": "фон", "msg_id": "h",
          "timestamp": t0}]
        + [{"_id": ids[i], "session_id": "voice", "role": "user", "content": f"v{i}", "msg_id": f"v{i}",
            "timestamp": t0} for i in range(1, 5)])

    seen, cursor = [], {"before": "2026-10-14T23:00:00Z"}
    for _ in range(6):
        page = client.get("/sam/monitor/history", params={**cursor, "limit": 2}).json()
        seen += [e["msg_id"] for e in page["events"]]
        if not page["oldest"]:
            break
        cursor = {"before": page["oldest"], "before_id": page["oldest_id"]}
    assert sorted(seen) == ["h", "v1", "v2", "v3", "v4"]


def test_monitor_tasks_survive_a_mongo_error(app_client, monkeypatch):
    """Сбой чтения sam.tasks - пустой список и строка лога, а не 500 на каждом снимке монитора."""
    from gateway import main

    class _BrokenTasks:
        def find(self, *a, **k):
            raise RuntimeError("mongo down")

    class _BrokenDb:
        tasks = _BrokenTasks()

    client, _ = app_client
    monkeypatch.setattr(main, "_mongo_ok", True)
    monkeypatch.setattr(main, "_db", _BrokenDb())
    r = client.get("/sam/tasks")
    assert r.status_code == 200 and r.json() == []
