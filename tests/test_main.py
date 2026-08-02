import sys
from types import SimpleNamespace
from unittest.mock import Mock

import main


def test_create_desktop_server_loads_cache_before_waitress(monkeypatch):
    calls = []
    fake_flask = SimpleNamespace(
        app=object(),
        load_usdb_cache=lambda: calls.append("cache"),
    )
    fake_server = object()

    def server_factory(*args, **kwargs):
        calls.append("server")
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 43210
        return fake_server

    monkeypatch.setitem(sys.modules, "app", fake_flask)
    monkeypatch.setattr(main, "create_server", server_factory)

    assert main.create_desktop_server(43210) is fake_server
    assert calls == ["cache", "server"]


def test_request_worker_shutdown_sets_event_and_joins(monkeypatch):
    stop_event = Mock()
    worker_thread = Mock()
    worker_thread.is_alive.side_effect = [True, False]
    fake_state = SimpleNamespace(
        worker_stop_event=stop_event,
        worker_thread=worker_thread,
    )
    monkeypatch.setitem(sys.modules, "state", SimpleNamespace(state=fake_state))

    assert main.request_worker_shutdown(timeout=3) is True
    stop_event.set.assert_called_once_with()
    worker_thread.join.assert_called_once_with(timeout=3)


def test_main_closes_server_after_webview_returns(monkeypatch):
    fake_server = Mock()
    fake_thread = Mock()

    monkeypatch.setattr(main, "find_free_port", lambda: 43210)
    monkeypatch.setattr(main, "create_desktop_server", lambda port: fake_server)
    monkeypatch.setattr(main.threading, "Thread", lambda **kwargs: fake_thread)
    monkeypatch.setattr(main, "wait_for_server", lambda port: True)
    monkeypatch.setattr(main, "request_worker_shutdown", Mock(return_value=True))
    monkeypatch.setattr(main.webview, "create_window", Mock())
    monkeypatch.setattr(main.webview, "start", Mock())

    assert main.main() == 0
    fake_thread.start.assert_called_once_with()
    main.request_worker_shutdown.assert_called_once()
    fake_server.close.assert_called_once_with()
    fake_thread.join.assert_called_once()
