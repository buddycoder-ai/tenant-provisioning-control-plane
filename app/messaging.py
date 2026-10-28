import json
import logging
import os
import threading
import time
from datetime import timedelta

import pika
from sqlalchemy import select

from .db import OutboxEvent, utcnow
from .schemas import ResultEvent
from .services import DomainError, apply_result

LOG = logging.getLogger(__name__)
TASK_QUEUE, RESULT_QUEUE, DLQ = (
    "provisioning.tasks",
    "provisioning.results",
    "provisioning.dead-letter",
)


def retry_delay_seconds(attempt: int, base_ms: int = 250, max_ms: int = 30_000) -> float:
    """Bounded exponential delay; deterministic to keep retry behavior auditable."""
    return min(base_ms * (2 ** max(attempt - 1, 0)), max_ms) / 1000


def connection() -> pika.BlockingConnection:
    return pika.BlockingConnection(
        pika.URLParameters(os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F"))
    )


def declare(channel: pika.adapters.blocking_connection.BlockingChannel) -> None:
    channel.queue_declare(queue=TASK_QUEUE, durable=True)
    channel.queue_declare(queue=RESULT_QUEUE, durable=True)
    channel.queue_declare(queue=DLQ, durable=True)


class OutboxRelay(threading.Thread):
    daemon = True

    def __init__(self, factory, stop: threading.Event):
        super().__init__(name="outbox-relay")
        self.factory, self.stop = factory, stop

    def run(self) -> None:
        LOG.info("Outbox relay started")
        while not self.stop.is_set():
            try:
                with connection() as conn:
                    channel = conn.channel()
                    declare(channel)
                    channel.confirm_delivery()
                    while not self.stop.is_set():
                        with self.factory() as session:
                            event = session.scalar(
                                select(OutboxEvent)
                                .where(
                                    OutboxEvent.published_at.is_(None),
                                    OutboxEvent.next_attempt_at <= utcnow(),
                                )
                                .order_by(OutboxEvent.created_at)
                                .limit(1)
                            )
                            if event is None:
                                time.sleep(0.2)
                                continue
                            try:
                                channel.basic_publish(
                                    "",
                                    TASK_QUEUE,
                                    json.dumps(event.payload),
                                    pika.BasicProperties(delivery_mode=2, message_id=event.task_id),
                                    mandatory=True,
                                )
                                event.published_at = utcnow()
                                LOG.info(
                                    "Outbox event published",
                                    extra={
                                        "task_id": event.task_id,
                                        "event_id": event.id,
                                        "attempts": event.attempts,
                                    },
                                )
                            except pika.exceptions.AMQPError as exc:
                                event.attempts += 1
                                event.last_error = type(exc).__name__
                                event.next_attempt_at = utcnow() + timedelta(
                                    seconds=retry_delay_seconds(event.attempts)
                                )
                                LOG.warning(
                                    "Outbox event publish failed, will retry",
                                    extra={
                                        "task_id": event.task_id,
                                        "event_id": event.id,
                                        "attempt": event.attempts,
                                        "error": type(exc).__name__,
                                    },
                                )
                            session.commit()
            except pika.exceptions.AMQPError:
                LOG.warning("outbox broker unavailable; retrying")
                time.sleep(1)


class ResultConsumer(threading.Thread):
    daemon = True

    def __init__(self, factory, stop: threading.Event):
        super().__init__(name="result-consumer")
        self.factory, self.stop = factory, stop

    def run(self) -> None:
        LOG.info("Result consumer started")
        while not self.stop.is_set():
            try:
                with connection() as conn:
                    channel = conn.channel()
                    declare(channel)
                    for method, _, body in channel.consume(
                        RESULT_QUEUE, inactivity_timeout=1, auto_ack=False
                    ):
                        if self.stop.is_set():
                            break
                        if method is None:
                            continue
                        try:
                            event = ResultEvent.model_validate_json(body)
                            LOG.info(
                                "Processing result event",
                                extra={
                                    "event_id": event.event_id,
                                    "task_id": event.task_id,
                                    "tenant_id": event.tenant_id,
                                    "status": event.status,
                                },
                            )
                            with self.factory() as session:
                                applied = apply_result(session, event)
                                if applied:
                                    LOG.info(
                                        "Result applied successfully",
                                        extra={
                                            "event_id": event.event_id,
                                            "task_id": event.task_id,
                                            "tenant_id": event.tenant_id,
                                            "status": event.status,
                                        },
                                    )
                                else:
                                    LOG.info(
                                        "Result was duplicate or stale, safely ignored",
                                        extra={
                                            "event_id": event.event_id,
                                            "task_id": event.task_id,
                                            "tenant_id": event.tenant_id,
                                            "status": event.status,
                                        },
                                    )
                            channel.basic_ack(method.delivery_tag)
                        except (ValueError, DomainError) as exc:
                            LOG.warning(
                                "dead-lettering invalid result",
                                extra={
                                    "error": str(exc),
                                    "event_body": body.decode()
                                    if isinstance(body, bytes)
                                    else body,
                                },
                            )
                            channel.basic_publish(
                                "", DLQ, body, pika.BasicProperties(delivery_mode=2)
                            )
                            channel.basic_ack(method.delivery_tag)
                        except Exception:
                            LOG.exception("result processing failed; message will retry")
                            channel.basic_nack(method.delivery_tag, requeue=True)
                    channel.cancel()
            except pika.exceptions.AMQPError:
                LOG.warning("result broker unavailable; retrying")
                time.sleep(1)


def start_background_components(factory):
    stop = threading.Event()
    threads = [OutboxRelay(factory, stop), ResultConsumer(factory, stop)]
    for thread in threads:
        thread.start()
    return stop, threads
