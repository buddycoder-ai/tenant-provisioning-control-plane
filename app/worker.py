import argparse
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone

import pika

from .logging_config import setup_logging
from .messaging import RESULT_QUEUE, TASK_QUEUE, connection, declare

logger = logging.getLogger("worker")
setup_logging("worker")


def result(task: dict, status: str) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "task_id": task["id"],
        "tenant_id": task["tenant_id"],
        "status": status,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def run(fail_rate: float, min_delay_ms: int, max_delay_ms: int) -> None:
    logger.info(
        "Worker started",
        extra={
            "fail_rate": fail_rate,
            "min_delay_ms": min_delay_ms,
            "max_delay_ms": max_delay_ms,
        },
    )
    with connection() as conn:
        channel = conn.channel()
        declare(channel)
        channel.confirm_delivery()

        def handle(ch, method, _, body):
            try:
                task = json.loads(body)
                logger.info(
                    "Received task",
                    extra={
                        "task_id": task["id"],
                        "task_type": task["type"],
                        "tenant_id": task["tenant_id"],
                        "task_status": task["status"],
                    },
                )
                if not {"id", "type", "tenant_id", "status"}.issubset(task):
                    raise ValueError("invalid task")
                for status in ("in_progress", "failed" if random.random() < fail_rate else "done"):
                    if status != "in_progress":
                        time.sleep(random.randint(min_delay_ms, max_delay_ms) / 1000)
                    result_event = result(task, status)
                    logger.info(
                        f"Publishing result: {status}",
                        extra={
                            "task_id": task["id"],
                            "tenant_id": task["tenant_id"],
                            "event_id": result_event["event_id"],
                            "result_status": status,
                        },
                    )
                    ch.basic_publish(
                        "",
                        RESULT_QUEUE,
                        json.dumps(result_event),
                        pika.BasicProperties(delivery_mode=2),
                    )
                logger.info(
                    "Task completed", extra={"task_id": task["id"], "tenant_id": task["tenant_id"]}
                )
                ch.basic_ack(method.delivery_tag)
            except Exception as exc:
                logger.error("Task processing failed", extra={"error": str(exc)}, exc_info=True)
                ch.basic_nack(method.delivery_tag, requeue=False)

        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(TASK_QUEUE, handle)
        channel.start_consuming()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-rate", type=float, default=0.0)
    parser.add_argument("--min-delay-ms", type=int, default=500)
    parser.add_argument("--max-delay-ms", type=int, default=2000)
    args = parser.parse_args()
    if (
        not 0 <= args.fail_rate <= 1
        or args.min_delay_ms < 0
        or args.max_delay_ms < args.min_delay_ms
    ):
        parser.error("fail-rate must be 0..1 and delays must satisfy 0 <= min <= max")
    while True:
        try:
            run(args.fail_rate, args.min_delay_ms, args.max_delay_ms)
        except pika.exceptions.AMQPError:
            # Compose may start us just before RabbitMQ accepts connections.
            time.sleep(1)


if __name__ == "__main__":
    main()
