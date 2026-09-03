from __future__ import annotations

import qt_app


def test_thumbnail_worker_pool_is_bounded(monkeypatch) -> None:
    monkeypatch.delenv("PDF_SIZE_REDUCER_WORKERS", raising=False)
    assert qt_app._thumbnail_worker_count(0) == 0
    assert 1 <= qt_app._thumbnail_worker_count(20) <= 4

    monkeypatch.setenv("PDF_SIZE_REDUCER_WORKERS", "2")
    assert qt_app._thumbnail_worker_count(20) == 2
    assert qt_app._thumbnail_worker_count(1) == 1
