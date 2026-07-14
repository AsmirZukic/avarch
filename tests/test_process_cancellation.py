import threading
import time

from avarch.models.execution import ProcessCancellationToken


def test_cancellation_token_starts_unset() -> None:
    token = ProcessCancellationToken()

    assert token.cancel_requested is False
    assert token.reason is None


def test_cancellation_token_can_be_requested_once_with_reason() -> None:
    token = ProcessCancellationToken()

    assert token.request("user requested stop") is True
    assert token.cancel_requested is True
    assert token.reason == "user requested stop"
    assert token.request("later reason") is False
    assert token.reason == "user requested stop"


def test_cancellation_token_request_is_observable_from_another_thread() -> None:
    token = ProcessCancellationToken()
    observed = threading.Event()

    def worker() -> None:
        if token.wait(timeout_seconds=1.0):
            observed.set()

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.05)
    token.request()
    thread.join(timeout=2.0)

    assert observed.is_set()
    assert token.cancel_requested is True


def test_cancellation_token_wait_times_out_when_unset() -> None:
    token = ProcessCancellationToken()

    assert token.wait(timeout_seconds=0.01) is False
