import asyncio

import pytest

import nodi.api as api
from nodi.api import BodyLimit


@pytest.mark.parametrize("value", [0, -1, True, None, float("nan"), float("inf")])
def test_invalid_timeout(value):
    with pytest.raises(ValueError):
        BodyLimit(None, read_timeout=value)


def test_stalled_receive_cancelled_before_app():
    async def scenario():
        called, sent, cancelled = [], [], []

        async def app(*args):
            called.append(True)

        async def receive():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        async def send(message):
            sent.append(message)

        await BodyLimit(app, read_timeout=0.01)({"type": "http"}, receive, send)
        assert not called and cancelled == [True]
        assert sent[0]["status"] == 408
        assert b"read deadline" in sent[1]["body"]

    asyncio.run(scenario())


def test_chunks_share_one_deadline(monkeypatch):
    times = iter([0, 0, 0.2, 0.3, 0.5, 0.6, 0.8, 1.1])
    monkeypatch.setattr(api, "monotonic", lambda: next(times))

    async def scenario():
        called, sent, budgets = [], [], []
        original_wait = asyncio.wait_for

        async def wait(awaitable, timeout):
            budgets.append(timeout)
            return await original_wait(awaitable, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", wait)

        async def app(*args):
            called.append(True)

        async def receive():
            return {"type": "http.request", "body": b" ", "more_body": True}

        async def send(message):
            sent.append(message)

        await BodyLimit(app, read_timeout=1)({"type": "http"}, receive, send)
        assert not called and sent[0]["status"] == 408
        assert budgets == pytest.approx([1, 0.7, 0.4])

    asyncio.run(scenario())


def test_disconnect_has_no_response_or_app_call():
    async def scenario():
        async def unexpected(*args):
            pytest.fail("disconnected request reached app or response")

        async def receive():
            return {"type": "http.disconnect"}

        await BodyLimit(unexpected)({"type": "http"}, receive, unexpected)

    asyncio.run(scenario())


def test_outer_cancellation_propagates():
    async def scenario():
        started, cancelled = asyncio.Event(), []

        async def unexpected(*args):
            pytest.fail("cancelled request reached app or response")

        async def receive():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        task = asyncio.create_task(BodyLimit(unexpected)({"type": "http"}, receive, unexpected))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == [True]

    asyncio.run(scenario())


def test_body_deadline_does_not_cancel_route():
    async def scenario():
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        observed = []

        async def app(scope, bounded_receive, send):
            await asyncio.sleep(0.03)
            observed.append(await bounded_receive())

        await BodyLimit(app, read_timeout=0.02)(
            {"type": "http", "headers": [(b"content-type", b"application/json")]}, receive, None
        )
        assert observed == [{"type": "http.request", "body": b"{}", "more_body": False}]

    asyncio.run(scenario())


def test_non_http_scope_passed_through():
    async def scenario():
        seen = []

        async def app(scope, receive, send):
            seen.append(scope)

        await BodyLimit(app)({"type": "lifespan"}, None, None)
        assert seen == [{"type": "lifespan"}]

    asyncio.run(scenario())
