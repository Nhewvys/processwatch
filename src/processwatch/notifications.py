"""Porta futura: eventos persistidos podem ser consumidos por cursor.

Somente kind=new é elegível por padrão. historical não cria evento;
corrected/review exigem política explícita. Entrega não está implementada.
"""
from typing import Protocol


class NotificationSink(Protocol):
    def send(self, event: dict, idempotency_key: str) -> None: ...
