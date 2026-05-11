"""Outgoing patient notifications (email).

The dev default is the console backend (emails print to runserver log).
Production sets EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
and supplies the EMAIL_HOST / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD vars.
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone


def _patient_email(appointment) -> str | None:
    """Best-effort lookup of the patient's email address."""
    p = appointment.patient
    if p.email:
        return p.email
    if p.user_id and p.user.email:
        return p.user.email
    return None


def send_appointment_confirmed(appointment) -> bool:
    """Send a templated confirmation email. Returns True if dispatched."""
    to = _patient_email(appointment)
    if not to:
        return False

    ctx = {
        'appointment': appointment,
        'patient':     appointment.patient,
        'doctor':      appointment.doctor,
        'department':  appointment.doctor.department,
        'now':         timezone.now(),
    }
    subject = (f'Appointment confirmed — Dr. {appointment.doctor.name} on '
               f'{timezone.localtime(appointment.date_time):%b %d, %Y at %I:%M %p}')
    body_text = render_to_string('email/appointment_confirmed.txt', ctx)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=body_text,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@smarthospital.local'),
        to=[to],
    )
    try:
        msg.send(fail_silently=False)
        return True
    except Exception:
        return False


def send_appointment_reminder(appointment) -> tuple[bool, str]:
    """Send a day-before reminder. Returns (sent?, target_email)."""
    to = _patient_email(appointment)
    if not to:
        return False, ''

    ctx = {
        'appointment': appointment,
        'patient':     appointment.patient,
        'doctor':      appointment.doctor,
        'department':  appointment.doctor.department,
        'now':         timezone.now(),
    }
    when = timezone.localtime(appointment.date_time)
    subject = f'Reminder — your visit with Dr. {appointment.doctor.name} tomorrow at {when:%I:%M %p}'
    body_text = render_to_string('email/appointment_reminder.txt', ctx)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=body_text,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@smarthospital.local'),
        to=[to],
    )
    try:
        msg.send(fail_silently=False)
        return True, to
    except Exception:
        return False, to
