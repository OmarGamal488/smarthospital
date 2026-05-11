from django.db import models

# Create your models here.

class Department(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name    


class Doctor(models.Model):
    user = models.OneToOneField(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='doctor',
        help_text='Linked user account when the doctor needs to sign in.',
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name='doctors'
    )
    name = models.CharField(max_length=150)
    specialty = models.CharField(max_length=100)
    email = models.EmailField(unique=True)

    def __str__(self):
        return f"Dr. {self.name} ({self.specialty})"
    
class DoctorAvailability(models.Model):
    """A recurring weekly window during which a doctor accepts appointments."""

    WEEKDAY_CHOICES = [
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'),
        (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    ]

    doctor       = models.ForeignKey(Doctor, on_delete=models.CASCADE, related_name='availabilities')
    weekday      = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    start_time   = models.TimeField()
    end_time     = models.TimeField()
    slot_minutes = models.PositiveSmallIntegerField(default=30)

    class Meta:
        unique_together = ('doctor', 'weekday', 'start_time')
        ordering = ['weekday', 'start_time']

    def __str__(self):
        return (f'{self.get_weekday_display()} '
                f'{self.start_time:%H:%M}–{self.end_time:%H:%M} '
                f'({self.slot_minutes}m) · {self.doctor.name}')


class DoctorTimeOff(models.Model):
    """A specific calendar date on which the doctor is unavailable.

    Overrides the recurring DoctorAvailability windows: when a date is listed
    here the booking flow shows zero open slots for that day.
    """

    doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, related_name='time_off')
    date   = models.DateField()
    reason = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('doctor', 'date')
        ordering = ['date']

    def __str__(self):
        return f'{self.doctor.name} off on {self.date:%Y-%m-%d}'


class Patient(models.Model):
    user = models.OneToOneField(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='patient',
        help_text='Linked user account when the patient self-registers.',
    )
    name = models.CharField(max_length=150)
    date_of_birth = models.DateField()
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)

    def __str__(self):
        return self.name
    
    
class Appointment(models.Model):
    STATUS_CHOICES = [
        ('Scheduled', 'Scheduled'),
        ('Completed', 'Completed'),
        ('Canceled',  'Canceled'),
    ]

    doctor = models.ForeignKey(
        Doctor,
        on_delete=models.PROTECT,
        related_name='appointments'
    )
    patient = models.ForeignKey(
        Patient,
        on_delete=models.PROTECT,
        related_name='appointments'
    )
    date_time = models.DateTimeField()
    reason = models.TextField()
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='Scheduled'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.patient} → {self.doctor} @ {self.date_time:%Y-%m-%d %H:%M}"

    @property
    def latest_prediction(self):
        return self.predictions.filter(status='SUCCESS').order_by('-created_at').first()

class AuditEvent(models.Model):
    """An immutable record of a meaningful change to the system."""

    ACTION_CHOICES = [
        ('create',  'Create'),
        ('update',  'Update'),
        ('delete',  'Delete'),
        ('analyze', 'Analyze'),
    ]
    KIND_CHOICES = [
        ('department',  'Department'),
        ('doctor',      'Doctor'),
        ('patient',     'Patient'),
        ('appointment', 'Appointment'),
        ('prediction',  'AI Prediction'),
    ]

    user = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='audit_events',
    )
    action     = models.CharField(max_length=10, choices=ACTION_CHOICES)
    kind       = models.CharField(max_length=15, choices=KIND_CHOICES)
    target_id  = models.IntegerField(null=True, blank=True)
    summary    = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['-created_at']), models.Index(fields=['kind'])]

    def __str__(self):
        actor = self.user.username if self.user_id else 'system'
        return f'{actor} · {self.action} {self.kind} #{self.target_id}'


class ChatSession(models.Model):
    """A persistent conversation for one user, optionally pinned to a page context."""

    KIND_CHOICES = [
        ('general',     'General'),
        ('dashboard',   'Dashboard'),
        ('patient',     'Patient'),
        ('appointment', 'Appointment'),
    ]

    user = models.ForeignKey(
        'auth.User',
        on_delete=models.CASCADE,
        related_name='chat_sessions',
    )
    title = models.CharField(max_length=120, blank=True)
    page_context_kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='general')
    page_context_id   = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.title or f'Chat #{self.pk}'


class ChatMessage(models.Model):
    """One turn in a chat session — user, assistant, or tool result."""

    ROLE_CHOICES = [
        ('user',      'User'),
        ('assistant', 'Assistant'),
        ('tool',      'Tool'),
    ]

    session = models.ForeignKey(
        ChatSession,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField(blank=True)
    tool_calls_json = models.TextField(blank=True)
    tool_call_id    = models.CharField(max_length=120, blank=True)
    tool_name       = models.CharField(max_length=80, blank=True)
    total_tokens    = models.IntegerField(default=0)
    latency_ms      = models.IntegerField(default=0)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.role}: {self.content[:50]}'


class VisitNote(models.Model):
    """Clinical notes the doctor records after an appointment.

    One-to-one with Appointment — a visit has at most one note. Separate from
    AIPrediction (which captures what the AI thinks) so the human record is
    never overwritten or mixed up with model output.
    """

    appointment = models.OneToOneField(
        Appointment,
        on_delete=models.CASCADE,
        related_name='visit_note',
    )
    note         = models.TextField(blank=True, help_text='Clinical observations from the visit.')
    prescription = models.TextField(blank=True, help_text='Medications / dosage / duration.')
    follow_up_needed = models.BooleanField(default=False)
    follow_up_date   = models.DateField(null=True, blank=True)
    created_by   = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='visit_notes',
    )
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'Visit note for Appt#{self.appointment_id}'


class Reminder(models.Model):
    """A 'we sent the patient a reminder' marker — idempotent guard for the
    `send_reminders` management command so it doesn't double-send."""

    KIND_CHOICES = [('email', 'Email'), ('telegram', 'Telegram')]
    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name='reminders',
    )
    kind     = models.CharField(max_length=20, choices=KIND_CHOICES, default='email')
    sent_at  = models.DateTimeField(auto_now_add=True)
    target   = models.CharField(max_length=120, blank=True, help_text='Email address or chat id we sent to.')

    class Meta:
        unique_together = ('appointment', 'kind')
        ordering = ['-sent_at']

    def __str__(self):
        return f'{self.kind} reminder for Appt#{self.appointment_id}'


class TelegramLink(models.Model):
    """Pairs a Django User (patient) with their Telegram chat. Created when the
    patient runs /link <code> in the bot using a code shown in their profile."""

    user      = models.OneToOneField(
        'auth.User', on_delete=models.CASCADE, related_name='telegram_link',
    )
    chat_id   = models.BigIntegerField(unique=True)
    username  = models.CharField(max_length=64, blank=True)
    link_code = models.CharField(max_length=12, unique=True,
                                  help_text='One-time code shown on the patient profile to claim the bot.')
    linked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'TG@{self.chat_id} → {self.user.username}'


class AppointmentRating(models.Model):
    """A 1–5 star rating + optional comment submitted by the patient AFTER a
    Completed appointment. One per appointment. Aggregated for doctor /
    department insights.
    """

    STARS = [(i, f'{i} star{"s" if i != 1 else ""}') for i in range(1, 6)]

    appointment = models.OneToOneField(
        Appointment, on_delete=models.CASCADE, related_name='rating',
    )
    stars      = models.PositiveSmallIntegerField(choices=STARS)
    comment    = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['-created_at'])]

    def __str__(self):
        return f'{self.stars}★ for Appt#{self.appointment_id}'


class AIPrediction(models.Model):
    STATUS_CHOICES = [
        ('SUCCESS', 'Success'),
        ('FAILED',  'Failed'),
    ]

    appointment = models.ForeignKey(
        Appointment,
        on_delete=models.CASCADE,
        related_name='predictions'
    )
    predicted_diagnosis = models.TextField()
    confidence_score = models.FloatField()
    model_version = models.CharField(max_length=50)
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='SUCCESS'
    )
    error_message = models.TextField(blank=True)
    latency_ms = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Prediction for Appt#{self.appointment_id} — {self.status}"