import main


def test_failing_event_callback_does_not_escape_dispatch():
    ha = main.HAClient("ws://x", "t")
    seen = []

    def bad(ev):
        raise KeyError("boom")

    ha._subs = {1: bad, 2: seen.append}
    # An exception here used to propagate into run(), which dropped the websocket and every subscription.
    ha._dispatch([{"id": 1, "type": "event", "event": {}}, {"id": 2, "type": "event", "event": {"x": 1}}])
    assert seen == [{"x": 1}]
