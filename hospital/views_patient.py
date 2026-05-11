"""Patient-facing views (live under /patient/...).

Patients are non-staff Users with a OneToOne link to a Patient row.
All views in this module use @patient_required and scope querysets to
request.user.patient. The chatbot's system prompt branches on user role
in chat_service._system_prompt — see that module.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .ai_service import predict_diagnosis, refine_symptoms
from .booking import available_slots, next_available_days
from .forms import PatientProfileForm, PatientRegistrationForm
from .models import AIPrediction, Appointment, AppointmentRating, Department, Doctor, TelegramLink
from .permissions import patient_required


# ── REGISTRATION + LOGIN ───────────────────────────────────────


def patient_register(request):
    if request.user.is_authenticated:
        if request.user.is_staff:
            return redirect('dashboard')
        return redirect('patient_home')
    form = PatientRegistrationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        auth_login(request, user)
        messages.success(
            request,
            f'Welcome, {user.patient.name.split()[0]}! Your account is ready.',
        )
        return redirect('patient_home')
    return render(request, 'hospital/patient/register.html', {'form': form})


# ── HOME / DASHBOARD ───────────────────────────────────────────


@patient_required
def patient_home(request):
    me = request.user.patient
    now = timezone.now()
    upcoming = (me.appointments
                  .select_related('doctor', 'doctor__department')
                  .filter(date_time__gte=now, status='Scheduled')
                  .order_by('date_time')[:5])
    recent = (me.appointments
                .select_related('doctor', 'doctor__department')
                .prefetch_related('predictions')
                .filter(date_time__lt=now)
                .order_by('-date_time')[:5])
    next_appt = upcoming.first()
    # Surface completed-but-unrated visits — a "please rate" nudge on the dashboard.
    pending_ratings = (me.appointments
                         .select_related('doctor')
                         .filter(status='Completed', rating__isnull=True)
                         .order_by('-date_time')[:3])
    return render(request, 'hospital/patient/dashboard.html', {
        'me':         me,
        'next_appt':  next_appt,
        'upcoming':   upcoming,
        'recent':     recent,
        'total':      me.appointments.count(),
        'pending_ratings': pending_ratings,
    })


# ── BOOKING FLOW ───────────────────────────────────────────────


@patient_required
def patient_book(request):
    """Step 1 — pick a department. Show every department (we'll still show
    each doctor in step 2 even if they have no availability set yet, with a
    badge — patients shouldn't see fewer choices because of a config gap)."""
    departments = (Department.objects
                   .annotate(num_doctors=Count('doctors'))
                   .filter(num_doctors__gt=0)
                   .order_by('name'))
    return render(request, 'hospital/patient/book.html', {
        'step':        1,
        'departments': departments,
    })


@patient_required
def patient_book_doctor(request, dept_pk):
    """Step 2 — pick a doctor in the chosen department.

    Every doctor in the department is shown. Doctors without any availability
    set are surfaced with a `bookable=False` flag so the template can mark
    them visually rather than hide them.
    """
    department = get_object_or_404(Department, pk=dept_pk)
    doctors = (department.doctors
               .select_related('department')
               .order_by('name'))
    doctors_view = []
    today = timezone.localdate()
    for doc in doctors:
        next_days = next_available_days(doc, n=2)
        doctors_view.append({
            'obj':        doc,
            'bookable':   bool(next_days),
            'next_day':   next_days[0] if next_days else None,
        })
    return render(request, 'hospital/patient/book_doctor.html', {
        'step':       2,
        'department': department,
        'doctors':    doctors_view,
    })


@patient_required
def patient_book_slots(request, doctor_pk):
    """Step 3 — pick a date & open slot for a doctor."""
    doctor = get_object_or_404(Doctor.objects.select_related('department'), pk=doctor_pk)

    raw_date = request.GET.get('date') or ''
    try:
        target_date = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        target_date = None
    if target_date is None:
        days = next_available_days(doctor, n=14)
        target_date = days[0] if days else timezone.localdate()

    slots = available_slots(doctor, target_date)

    return render(request, 'hospital/patient/book_slots.html', {
        'step':         3,
        'doctor':       doctor,
        'target_date':  target_date,
        'slots':        slots,
        'next_days':    next_available_days(doctor, n=14),
    })


@patient_required
@require_POST
def patient_book_confirm(request, doctor_pk):
    """Step 4 — create the appointment from a chosen slot + reason."""
    doctor = get_object_or_404(Doctor.objects.select_related('department'), pk=doctor_pk)
    slot_iso = (request.POST.get('slot') or '').strip()
    reason   = (request.POST.get('reason') or '').strip()

    if not slot_iso or not reason:
        messages.error(request, 'Please choose a time slot and describe the reason.')
        return redirect('patient_book_slots', doctor_pk=doctor.pk)

    try:
        slot_dt = datetime.fromisoformat(slot_iso)
    except ValueError:
        messages.error(request, 'Invalid time slot.')
        return redirect('patient_book_slots', doctor_pk=doctor.pk)

    if timezone.is_naive(slot_dt):
        slot_dt = timezone.make_aware(slot_dt, timezone.get_current_timezone())

    # Re-validate the slot is still open (race-condition guard).
    if slot_dt not in available_slots(doctor, slot_dt.date()):
        messages.error(request, 'That slot was just taken. Please pick another.')
        return redirect('patient_book_slots', doctor_pk=doctor.pk)

    appt = Appointment.objects.create(
        doctor=doctor,
        patient=request.user.patient,
        date_time=slot_dt,
        reason=reason,
        status='Scheduled',
    )

    # Run AI prediction (graceful — failures are persisted as FAILED)
    try:
        result = predict_diagnosis(appt)
    except Exception as exc:
        result = {
            'predicted_diagnosis': '', 'confidence_score': 0.0,
            'model_version': 'unknown', 'status': 'FAILED',
            'error_message': str(exc), 'latency_ms': 0, 'total_tokens': 0,
        }
    AIPrediction.objects.create(appointment=appt, **result)

    # Email confirmation (Phase 6 — best-effort)
    try:
        from .notifications import send_appointment_confirmed
        send_appointment_confirmed(appt)
    except Exception:
        pass

    messages.success(
        request,
        f'Booked! {slot_dt.strftime("%A, %b %d at %I:%M %p")} '
        f'with Dr. {doctor.name}.',
    )
    return redirect('patient_appointment_detail', pk=appt.pk)


# ── AI symptom helper (used by the booking reason textarea) ───


@patient_required
@require_POST
def patient_ai_symptoms(request):
    """Take the patient's rough symptom note + optional doctor pk, return an
    AI-refined version. Pure JSON endpoint — used by JS on book_slots.html.
    """
    import json
    try:
        payload = json.loads((request.body or b'{}').decode('utf-8'))
    except Exception:
        return JsonResponse({'status': 'FAILED', 'error_message': 'Invalid JSON.'}, status=400)
    text = (payload.get('text') or '').strip()
    doctor_pk = payload.get('doctor_pk')
    specialty = ''
    if doctor_pk:
        doc = Doctor.objects.filter(pk=doctor_pk).first()
        if doc:
            specialty = doc.specialty
    result = refine_symptoms(text, specialty=specialty)
    return JsonResponse(result)


# ── APPOINTMENTS ──────────────────────────────────────────────


@patient_required
def patient_appointments(request):
    me = request.user.patient
    qs = (me.appointments
            .select_related('doctor', 'doctor__department')
            .prefetch_related('predictions')
            .order_by('-date_time'))
    return render(request, 'hospital/patient/appointments.html', {
        'appointments': qs,
        'now':          timezone.now(),
    })


@patient_required
def patient_appointment_detail(request, pk):
    me = request.user.patient
    appt = get_object_or_404(
        Appointment.objects
            .select_related('doctor', 'doctor__department')
            .prefetch_related('predictions')
            .filter(patient=me),
        pk=pk,
    )
    return render(request, 'hospital/patient/appointment_detail.html', {
        'appt':        appt,
        'predictions': appt.predictions.order_by('-created_at'),
        'now':         timezone.now(),
    })


@patient_required
def patient_appointment_rate(request, pk):
    """Patient submits / edits a star rating for a completed appointment."""
    me = request.user.patient
    appt = get_object_or_404(
        Appointment.objects.select_related('doctor', 'doctor__department').filter(patient=me),
        pk=pk,
    )
    if appt.status != 'Completed':
        messages.error(request, 'You can only rate a visit after it has been completed.')
        return redirect('patient_appointment_detail', pk=appt.pk)

    existing = AppointmentRating.objects.filter(appointment=appt).first()
    if request.method == 'POST':
        try:
            stars = int(request.POST.get('stars') or 0)
        except ValueError:
            stars = 0
        if stars < 1 or stars > 5:
            messages.error(request, 'Please choose a rating between 1 and 5 stars.')
            return redirect('patient_appointment_rate', pk=appt.pk)
        comment = (request.POST.get('comment') or '').strip()[:1000]
        if existing:
            existing.stars   = stars
            existing.comment = comment
            existing.save(update_fields=['stars', 'comment', 'updated_at'])
            messages.success(request, 'Thanks — your rating was updated.')
        else:
            AppointmentRating.objects.create(appointment=appt, stars=stars, comment=comment)
            messages.success(request, f'Thanks for rating Dr. {appt.doctor.name}!')
        return redirect('patient_appointment_detail', pk=appt.pk)

    return render(request, 'hospital/patient/rate.html', {
        'appt':     appt,
        'existing': existing,
    })


@patient_required
def patient_appointment_reschedule(request, pk):
    """Let the patient pick a new open slot for one of their own appointments."""
    me = request.user.patient
    appt = get_object_or_404(
        Appointment.objects.select_related('doctor', 'doctor__department').filter(patient=me),
        pk=pk,
    )
    if appt.status != 'Scheduled' or appt.date_time <= timezone.now():
        messages.error(request, 'This appointment cannot be rescheduled.')
        return redirect('patient_appointment_detail', pk=appt.pk)

    doctor = appt.doctor
    raw_date = request.GET.get('date') or ''
    try:
        target_date = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        target_date = None
    if target_date is None:
        days = next_available_days(doctor, n=14)
        target_date = days[0] if days else timezone.localdate()

    slots = available_slots(doctor, target_date)

    if request.method == 'POST':
        slot_iso = (request.POST.get('slot') or '').strip()
        try:
            slot_dt = datetime.fromisoformat(slot_iso)
        except ValueError:
            messages.error(request, 'Pick a valid time slot.')
            return redirect('patient_appointment_reschedule', pk=appt.pk)
        if timezone.is_naive(slot_dt):
            slot_dt = timezone.make_aware(slot_dt, timezone.get_current_timezone())
        if slot_dt not in available_slots(doctor, slot_dt.date()):
            messages.error(request, 'That slot was just taken. Please pick another.')
            return redirect('patient_appointment_reschedule', pk=appt.pk)
        old = appt.date_time
        appt.date_time = slot_dt
        appt.save(update_fields=['date_time'])
        messages.success(
            request,
            f'Rescheduled from {timezone.localtime(old).strftime("%b %d, %I:%M %p")} '
            f'to {slot_dt.strftime("%b %d, %I:%M %p")}.',
        )
        return redirect('patient_appointment_detail', pk=appt.pk)

    return render(request, 'hospital/patient/reschedule.html', {
        'appt':        appt,
        'doctor':      doctor,
        'target_date': target_date,
        'slots':       slots,
        'next_days':   next_available_days(doctor, n=14),
    })


@patient_required
@require_POST
def patient_appointment_cancel(request, pk):
    me = request.user.patient
    appt = get_object_or_404(Appointment.objects.filter(patient=me), pk=pk)
    if appt.status == 'Scheduled' and appt.date_time > timezone.now():
        appt.status = 'Canceled'
        appt.save(update_fields=['status'])
        messages.success(request, 'Appointment canceled.')
    else:
        messages.error(request, 'This appointment cannot be canceled.')
    return redirect('patient_appointments')


# ── PROFILE ───────────────────────────────────────────────────


@patient_required
def patient_profile(request):
    me = request.user.patient
    form = PatientProfileForm(request.POST or None, instance=me)
    if request.method == 'POST' and request.POST.get('form_kind') != 'tg' and form.is_valid():
        form.save()
        messages.success(request, 'Profile updated.')
        return redirect('patient_profile')
    tg_link = TelegramLink.objects.filter(user=request.user).first()
    return render(request, 'hospital/patient/profile.html', {
        'form': form, 'me': me, 'tg_link': tg_link,
    })


@patient_required
@require_POST
def patient_telegram_code(request):
    """(Re)generate the patient's one-time Telegram link code.

    If the patient is already linked, this is a no-op. Otherwise we rotate the
    code so an old screenshot can't be reused.
    """
    from .telegram_bot import _new_code
    existing = TelegramLink.objects.filter(user=request.user).first()
    if existing and existing.linked_at:
        messages.info(request, 'Your Telegram account is already linked.')
        return redirect('patient_profile')
    code = _new_code()
    # Ensure uniqueness (8-char codes are plenty, but be defensive)
    while TelegramLink.objects.filter(link_code=code).exists():
        code = _new_code()
    if existing:
        existing.link_code = code
        existing.save(update_fields=['link_code'])
    else:
        TelegramLink.objects.create(user=request.user, chat_id=-int(request.user.pk), link_code=code)
        # chat_id is overwritten when /link is received. We use a negative
        # placeholder here so the unique constraint stays satisfied.
    messages.success(request, 'Telegram link code generated — copy it below.')
    return redirect('patient_profile')


# ── PUBLIC: services / departments ────────────────────────────


def services(request):
    departments = (Department.objects
                   .prefetch_related('doctors')
                   .order_by('name'))
    return render(request, 'hospital/patient/services.html', {
        'departments': departments,
    })


def services_department(request, pk):
    department = get_object_or_404(Department, pk=pk)
    doctors = (department.doctors.select_related('department')
                                  .order_by('name'))
    return render(request, 'hospital/patient/services_department.html', {
        'department': department,
        'doctors':    doctors,
    })
