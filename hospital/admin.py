import secrets
import string

from django.contrib import admin, messages as dj_messages
from django.contrib.auth.models import User

from .models import (
    AIPrediction,
    Appointment,
    AuditEvent,
    ChatMessage,
    ChatSession,
    Department,
    Doctor,
    DoctorAvailability,
    Patient,
)


def _random_password(n: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits + '!@#$%&*'
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def _slug_username(name: str) -> str:
    base = 'dr.' + ''.join(c.lower() if c.isalnum() else '.' for c in name).strip('.')
    base = '.'.join(filter(None, base.split('.')))[:30]
    # Ensure uniqueness
    candidate = base
    i = 1
    while User.objects.filter(username=candidate).exists():
        i += 1
        candidate = f'{base}.{i}'[:30]
    return candidate


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'short_description', 'doctor_count')
    search_fields = ('name',)

    @admin.display(description='Description')
    def short_description(self, obj):
        return obj.description[:60] + '…' if len(obj.description) > 60 else obj.description

    @admin.display(description='Doctors')
    def doctor_count(self, obj):
        return obj.doctors.count()


class DoctorAvailabilityInline(admin.TabularInline):
    model = DoctorAvailability
    extra = 1
    fields = ('weekday', 'start_time', 'end_time', 'slot_minutes')


@admin.register(Doctor)
class DoctorAdmin(admin.ModelAdmin):
    list_display = ('name', 'specialty', 'department', 'email', 'user')
    list_filter = ('department',)
    search_fields = ('name', 'specialty', 'email', 'user__username')
    list_select_related = ('department', 'user')
    inlines = [DoctorAvailabilityInline]
    actions = ['create_login_for_doctors']

    @admin.action(description='Create a login account for selected doctors')
    def create_login_for_doctors(self, request, queryset):
        created, skipped = [], []
        for doc in queryset:
            if doc.user_id:
                skipped.append(doc.name)
                continue
            username = _slug_username(doc.name)
            password = _random_password()
            user = User.objects.create_user(
                username=username,
                email=doc.email or '',
                password=password,
            )
            user.is_staff = False
            user.save()
            doc.user = user
            doc.save(update_fields=['user'])
            created.append((doc.name, username, password))

        if created:
            lines = [f'Created {len(created)} doctor login(s). Save these now — passwords shown ONCE:']
            for name, u, p in created:
                lines.append(f'  · {name} → username "{u}" / password "{p}"')
            self.message_user(request, '\n'.join(lines), level=dj_messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f'Skipped {len(skipped)} doctor(s) who already have a user: {", ".join(skipped)}',
                level=dj_messages.WARNING,
            )


@admin.register(DoctorAvailability)
class DoctorAvailabilityAdmin(admin.ModelAdmin):
    list_display = ('doctor', 'weekday', 'start_time', 'end_time', 'slot_minutes')
    list_filter = ('weekday', 'doctor')
    list_select_related = ('doctor',)


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ('name', 'date_of_birth', 'phone', 'email', 'user')
    search_fields = ('name', 'phone', 'email', 'user__username')
    list_select_related = ('user',)


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ('patient', 'doctor', 'date_time', 'status', 'created_at')
    list_filter = ('status', 'doctor')
    search_fields = ('patient__name', 'doctor__name', 'reason')
    date_hierarchy = 'date_time'
    list_select_related = ('doctor', 'patient')


@admin.register(AIPrediction)
class AIPredictionAdmin(admin.ModelAdmin):
    list_display = ('appointment', 'status', 'short_diagnosis', 'confidence_score', 'model_version', 'latency_ms', 'total_tokens', 'created_at')
    list_filter = ('status', 'model_version')
    list_select_related = ('appointment',)
    readonly_fields = ('appointment', 'predicted_diagnosis', 'confidence_score', 'model_version',
                       'status', 'error_message', 'latency_ms', 'total_tokens', 'created_at')

    @admin.display(description='Diagnosis')
    def short_diagnosis(self, obj):
        return obj.predicted_diagnosis[:50] + '…' if len(obj.predicted_diagnosis) > 50 else obj.predicted_diagnosis


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    fields = ('role', 'content_short', 'tool_name', 'total_tokens', 'created_at')
    readonly_fields = ('role', 'content_short', 'tool_name', 'total_tokens', 'created_at')
    extra = 0
    can_delete = False

    @admin.display(description='Content')
    def content_short(self, obj):
        return (obj.content[:80] + '…') if len(obj.content) > 80 else obj.content


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'title', 'page_context_kind', 'page_context_id', 'message_count', 'updated_at')
    list_filter = ('page_context_kind',)
    search_fields = ('user__username', 'title')
    list_select_related = ('user',)
    inlines = [ChatMessageInline]

    @admin.display(description='Messages')
    def message_count(self, obj):
        return obj.messages.count()


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'user', 'action', 'kind', 'target_id', 'summary')
    list_filter = ('action', 'kind')
    search_fields = ('summary', 'user__username')
    list_select_related = ('user',)
    readonly_fields = ('user', 'action', 'kind', 'target_id', 'summary', 'created_at')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'session', 'role', 'tool_name', 'short_content', 'total_tokens', 'created_at')
    list_filter = ('role',)
    list_select_related = ('session',)
    search_fields = ('content', 'tool_name')

    @admin.display(description='Content')
    def short_content(self, obj):
        return (obj.content[:60] + '…') if len(obj.content) > 60 else obj.content
