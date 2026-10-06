"""Thin wrappers around the Kafka client (confluent-kafka)."""

import json

from confluent_kafka import Consumer, Producer
from confluent_kafka.admin import AdminClient, NewTopic

from app.db.models import OutboxEvent
from app.events.outbox import as_message


class PublishFailed(Exception):
    pass


class KafkaPublisher:
    def __init__(self, bootstrap: str, topic: str):
        self.topic = topic
        # acks=all: the broker confirms only once the message is safely stored.
        # enable.idempotence: the client's own retries never write a message twice.
        self.producer = Producer({"bootstrap.servers": bootstrap, "acks": "all", "enable.idempotence": True})

    def __call__(self, events: list[OutboxEvent]) -> None:
        errors = []

        def on_delivery(error, message):
            if error is not None:
                errors.append(error)

        for event in events:
            self.producer.produce(
                self.topic,
                key=str(event.entry_id),
                value=json.dumps(as_message(event)),
                on_delivery=on_delivery,
            )
        not_delivered = self.producer.flush(timeout=10)
        if errors or not_delivered:
            raise PublishFailed(f"{len(errors)} failed, {not_delivered} not acknowledged")


def ensure_topic(bootstrap: str, topic: str) -> None:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    if topic in admin.list_topics(timeout=10).topics:
        return
    futures = admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    try:
        futures[topic].result(timeout=10)
    except Exception as error:  # another process created it first
        if "TOPIC_ALREADY_EXISTS" not in str(error):
            raise


def make_consumer(bootstrap: str, topic: str, group: str) -> Consumer:
    # Offsets are committed by hand, only after the database work for a message has committed.
    consumer = Consumer({
        "bootstrap.servers": bootstrap,
        "group.id": group,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
    })
    consumer.subscribe([topic])
    return consumer
