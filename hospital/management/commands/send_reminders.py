"""Send 24h-before email reminders for upcoming scheduled appointments.

Picks every Scheduled appointment whose `date_time` falls in the next
[lower, upper] window (default 18–30h from now), skipping any that already
have a Reminder(kind='email') row. Each successful send is recorded so the
command is idempotent — safe to run hourly via cron / systemd timer.

Usage:
    python manage.py send_reminders                  # default 18–30h window
    python manage.py send_reminders --lower 12 --upper 36
    python manage.py send_reminders --dry-run         # don't actually send
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from hospital.models import Appointment, Reminder
from hospital.notifications import send_appointment_reminder


class Command(BaseCommand):
    help = 'Send day-before email reminders for upcoming appointments.'

    def add_arguments(self, parser):
        parser.add_argument('--lower', type=int, default=18,
                            help='Lower bound in hours from now (default 18).')
        parser.add_argument('--upper', type=int, default=30,
                            help='Upper bound in hours from now (default 30).')
        parser.add_argument('--dry-run', action='store_true',
                            help="Don't send or persist; just report what would be sent.")

    def handle(self, *args, **opts):
        now    = timezone.now()
        lower  = now + timedelta(hours=opts['lower'])
        upper  = now + timedelta(hours=opts['upper'])
        dry    = opts['dry_run']

        qs = (Appointment.objects
              .select_related('patient', 'patient__user', 'doctor', 'doctor__department')
              .filter(status='Scheduled',
                      date_time__gte=lower,
                      date_time__lte=upper)
              .exclude(reminders__kind='email'))

        sent_n   = 0
        skip_n   = 0
        fail_n   = 0
        for appt in qs:
            if dry:
                self.stdout.write(f'[dry] would remind appt #{appt.pk} '
                                  f'({appt.patient.name} @ {appt.date_time:%b %d %I:%M %p})')
                continue
            ok, target = send_appointment_reminder(appt)
            if not target:
                skip_n += 1
                continue
            if ok:
                Reminder.objects.create(appointment=appt, kind='email', target=target)
                sent_n += 1
            else:
                fail_n += 1
                self.stdout.write(self.style.WARNING(
                    f'Failed to send reminder for appt #{appt.pk} → {target}'))

        if dry:
            self.stdout.write(self.style.SUCCESS(
                f'[dry] {qs.count()} appointment(s) would have been reminded.'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Reminders: {sent_n} sent · {skip_n} skipped (no email) · {fail_n} failed.'
            ))
