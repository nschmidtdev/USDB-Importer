import login_window


def test_create_login_window_does_not_start_second_event_loop(monkeypatch):
    created = []
    started = []

    class LoadedEvent:
        def __iadd__(self, callback):
            self.callback = callback
            return self

    class Window:
        def __init__(self):
            self.events = type("Events", (), {"loaded": LoadedEvent()})()

    def create_window(*args, **kwargs):
        window = Window()
        created.append((args, kwargs, window))
        return window

    monkeypatch.setattr(login_window.webview, "create_window", create_window)
    monkeypatch.setattr(login_window.webview, "start", lambda: started.append(True))

    window = login_window.create_login_window("one-time-token")

    assert window is created[0][2]
    assert login_window.TRANSFER_TOKEN == "one-time-token"
    assert started == []
