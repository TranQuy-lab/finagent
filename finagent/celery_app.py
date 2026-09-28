"""Lớp phân tán: Celery app của máy chủ + semaphore giới hạn slot toàn cục.

Máy chủ đẩy việc thu thập vào Redis; các máy con chạy ``celery worker`` lắng
nghe hàng đợi ``crawl`` và thực thi. Số việc chạy song song trên TOÀN BỘ máy con
được chặn trên bởi một semaphore Redis, nhờ vậy có thể thêm máy con tuỳ ý mà
không làm quá tải nguồn dữ liệu.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager

import redis
from celery import Celery

from finagent.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Celery app
# ---------------------------------------------------------------------------

celery_app = Celery(
    "finagent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["finagent.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Ho_Chi_Minh",
    enable_utc=True,
    # Mỗi máy con tự khai báo số tiến trình con; tổng slot vẫn bị chặn bởi semaphore.
    worker_concurrency=settings.worker_concurrency,
    worker_prefetch_multiplier=1,  # chia việc đều, tránh một máy con ôm hết
    task_acks_late=True,           # máy con chết giữa chừng thì việc được giao lại
    task_reject_on_worker_lost=True,
    task_time_limit=600,
    task_soft_time_limit=540,
    result_expires=3600,
    task_default_queue=settings.crawl_queue,
    task_routes={"finagent.tasks.*": {"queue": settings.crawl_queue}},
    broker_connection_retry_on_startup=True,
)


# ---------------------------------------------------------------------------
# Semaphore Redis: giới hạn tổng số slot thu thập trên toàn cụm máy con
# ---------------------------------------------------------------------------

class SlotPool:
    """Semaphore đếm bằng Redis, an toàn khi nhiều máy con cùng giành slot.

    Mỗi slot là một khoá Redis có TTL riêng. Nếu một máy con chết đột ngột,
    khoá của nó tự hết hạn nên slot được trả lại, cụm không bị kẹt.
    """

    def __init__(self, client: redis.Redis, key: str, capacity: int, ttl: int) -> None:
        self._client = client
        self._key = key
        self._capacity = capacity
        self._ttl = ttl

    def _member_keys(self) -> list[str]:
        return [k.decode() if isinstance(k, bytes) else k for k in self._client.smembers(self._key)]

    def _reap_expired(self, members: list[str]) -> None:
        """Dọn các slot đã hết hạn nhưng còn sót trong set."""
        for member in members:
            if not self._client.exists(member):
                self._client.srem(self._key, member)

    def acquire(self, wait_seconds: int) -> str | None:
        """Giành một slot; trả về mã slot, hoặc ``None`` nếu hết thời gian chờ."""
        deadline = time.monotonic() + max(wait_seconds, 0)
        token = f"finagent:slot:{uuid.uuid4().hex}"
        while True:
            members = self._member_keys()
            self._reap_expired(members)
            if len(members) < self._capacity:
                pipe = self._client.pipeline()
                pipe.set(token, "1", ex=self._ttl)
                pipe.sadd(self._key, token)
                pipe.expire(self._key, self._ttl * 2)
                pipe.execute()
                return token
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    def release(self, token: str) -> None:
        pipe = self._client.pipeline()
        pipe.delete(token)
        pipe.srem(self._key, token)
        pipe.execute()

    def in_use(self) -> int:
        members = self._member_keys()
        self._reap_expired(members)
        return len(members)

    def heartbeat(self, token: str) -> None:
        """Gia hạn slot khi tác vụ chạy lâu hơn TTL."""
        self._client.expire(token, self._ttl)


_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Trả về client Redis dùng chung (tạo một lần cho mỗi tiến trình)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=False,
            socket_timeout=10,
            health_check_interval=30,
        )
    return _redis_client


@contextmanager
def crawl_slot():
    """Ngữ cảnh giành/trả slot quanh một tác vụ thu thập.

    Nếu hết slot trong thời gian chờ, tác vụ được bỏ qua một cách êm ái thay vì
    xếp hàng vô hạn — dữ liệu thị trường luôn có lượt quét kế tiếp.
    """
    pool = SlotPool(
        get_redis(),
        key=settings.slot_lock_key,
        capacity=settings.max_concurrency,
        ttl=settings.slot_ttl_seconds,
    )
    token = pool.acquire(settings.slot_wait_seconds)
    if token is None:
        logger.warning("Hết slot thu thập (tối đa %d) — bỏ qua lượt này.", settings.max_concurrency)
        yield False
        return
    try:
        yield True
    finally:
        pool.release(token)


def cluster_status() -> dict:
    """Thông tin phục vụ lệnh ``finagent status``: slot đang dùng + máy con online.

    Máy con đăng ký bằng một hash có TTL (tự hết hạn nếu máy đó tắt). Tuy nhiên
    tập hợp tên máy con thì không tự hết hạn, nên phải chủ động loại những máy đã
    hết hạn — nếu không, danh sách sẽ còn mãi tên máy đã tắt và số máy con dùng để
    chia việc sẽ bị đếm sai.
    """
    client = get_redis()
    pool = SlotPool(client, settings.slot_lock_key, settings.max_concurrency, settings.slot_ttl_seconds)
    workers: list[dict] = []
    stale: list[str] = []
    try:
        for raw in client.smembers("finagent:workers"):
            name = raw.decode() if isinstance(raw, bytes) else raw
            info = client.hgetall(f"finagent:worker:{name}")
            if not info:
                stale.append(name)
                continue
            decoded = {
                (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in info.items()
            }
            workers.append({"name": name, **decoded})

        # Dọn tên máy con đã hết hạn để lần sau đếm đúng.
        if stale:
            client.srem("finagent:workers", *stale)
            logger.debug("Đã loại %d máy con không còn hoạt động: %s", len(stale), stale)
    except redis.RedisError as exc:  # pragma: no cover - phụ thuộc hạ tầng
        logger.warning("Không đọc được danh sách máy con: %s", exc)

    return {
        "slots_in_use": pool.in_use(),
        "slots_total": settings.max_concurrency,
        "workers": sorted(workers, key=lambda w: w["name"]),
        "queue": settings.crawl_queue,
    }


def dispatch(task, *args, **kwargs):
    """Đẩy một tác vụ sang hàng đợi cho máy con thực thi."""
    return task.apply_async(args=args, kwargs=kwargs, queue=settings.crawl_queue)
