"""Audit logging — signals that record create/update/delete events for
the four core models. The acting user is captured via a thread-local
populated by AuditMiddleware (request → response cycle).
"""

from __future__ import annotations

import threading
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import AIPrediction, Appointment, AuditEvent, Department, Doctor, Patient

_thread_locals = threading.local()


def _get_user():
    return getattr(_thread_locals, 'user', None)


def set_audit_user(user) -> None:
    _thread_locals.user = user if (user and user.is_authenticated) else None


def clear_audit_user() -> None:
    _thread_locals.user = None


class AuditMiddleware:
    """Stash the request user on a thread-local so signals can access it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_audit_user(getattr(request, 'user', None))
        try:
            response = self.get_response(request)
        finally:
            clear_audit_user()
        return response


_KIND_BY_MODEL = {
    Department:   'department',
    Doctor:       'doctor',
    Patient:      'patient',
    Appointment:  'appointment',
    AIPrediction: 'prediction',
}


def _summarize(instance: Any, action: str) -> str:
    if isinstance(instance, Department):
        return f'Department "{instance.name}"'
    if isinstance(instance, Doctor):
        return f'Dr. {instance.name} · {instance.specialty}'
    if isinstance(instance, Patient):
        return f'Patient {instance.name}'
    if isinstance(instance, Appointment):
        return f'{instance.patient.name} ↔ Dr. {instance.doctor.name} on {instance.date_time:%Y-%m-%d %H:%M}'
    if isinstance(instance, AIPrediction):
        if instance.status == 'SUCCESS':
            pct = int((instance.confidence_score or 0) * 100)
            dx = (instance.predicted_diagnosis or '')[:60]
            return f'AI · {dx} ({pct}%)'
        return f'AI failed: {instance.error_message[:80]}'
    return str(instance)[:120]


def _broadcast(event: AuditEvent) -> None:
    """Push the audit event to anyone connected to the 'updates' WS group."""
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)('updates', {
            'type':       'audit.event',
            'action':     event.action,
            'kind':       event.kind,
            'target_id':  event.target_id,
            'summary':    event.summary,
            'user':       event.user.username if event.user_id else None,
            'created_at': event.created_at.isoformat(),
        })
    except Exception:
        # WS broadcast must never break the main DB write
        pass


@receiver(post_save, sender=Department)
@receiver(post_save, sender=Doctor)
@receiver(post_save, sender=Patient)
@receiver(post_save, sender=Appointment)
@receiver(post_save, sender=AIPrediction)
def _on_save(sender, instance, created, **kwargs):
    kind = _KIND_BY_MODEL.get(sender)
    if kind is None:
        return
    if isinstance(instance, AIPrediction):
        action = 'analyze'
    else:
        action = 'create' if created else 'update'
    ev = AuditEvent.objects.create(
        user=_get_user(),
        action=action,
        kind=kind,
        target_id=instance.pk,
        summary=_summarize(instance, action),
    )
    _broadcast(ev)


@receiver(post_delete, sender=Department)
@receiver(post_delete, sender=Doctor)
@receiver(post_delete, sender=Patient)
@receiver(post_delete, sender=Appointment)
def _on_delete(sender, instance, **kwargs):
    kind = _KIND_BY_MODEL.get(sender)
    if kind is None:
        return
    ev = AuditEvent.objects.create(
        user=_get_user(),
        action='delete',
        kind=kind,
        target_id=instance.pk,
        summary=_summarize(instance, 'delete'),
    )
    _broadcast(ev)
