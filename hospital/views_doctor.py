"""Doctor-facing views (under /doctor/...).

Doctors are non-staff Users with a OneToOne link to a Doctor row. All views
here use @doctor_required and scope querysets to request.user.doctor.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Appointment, Doctor, DoctorAvailability, DoctorTimeOff, VisitNote
from .permissions import doctor_required


# ── HOME ──────────────────────────────────────────────────────


@doctor_required
def doctor_home(request):
    me = request.user.doctor
    now = timezone.now()
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today_start + timedelta(days=1)

    today_qs = (me.appointments
                  .select_related('patient')
                  .prefetch_related('predictions')
                  .filter(date_time__gte=today_start, date_time__lt=tomorrow)
                  .order_by('date_time'))
    # Compute live "phase" per appointment: done / now / next.
    # Uses a 20-minute "in-session" window from start time. We attach as
    # transient attributes so the template can read them straight off the obj.
    SESSION_MIN = 20
    upcoming_today_count = 0
    completed_today_count = 0
    in_progress_count = 0
    for a in today_qs:
        if a.status == 'Canceled':
            a.live_phase = 'canceled'
            continue
        if a.status == 'Completed':
            a.live_phase = 'completed'
            completed_today_count += 1
            continue
        start = a.date_time
        ends = start + timedelta(minutes=SESSION_MIN)
        if now < start:
            a.live_phase = 'upcoming'
            upcoming_today_count += 1
        elif start <= now < ends:
            a.live_phase = 'now'
            in_progress_count += 1
        else:
            a.live_phase = 'overdue'  # past start time, still scheduled — needs attention
            upcoming_today_count += 1

    upcoming = (me.appointments
                  .select_related('patient')
                  .filter(date_time__gte=tomorrow, status='Scheduled')
                  .order_by('date_time')[:5])
    recent = (me.appointments
                .select_related('patient')
                .prefetch_related('predictions')
                .filter(date_time__lt=today_start)
                .order_by('-date_time')[:5])
    total = me.appointments.count()
    unique_patients = me.appointments.values('patient').distinct().count()
    unique_patients_today = today_qs.exclude(status='Canceled').values('patient').distinct().count()

    # Next 7-day calendar: count of patients per day (excludes canceled).
    week_horizon = today_start + timedelta(days=7)
    next7_raw = (me.appointments
                   .filter(date_time__gte=today_start, date_time__lt=week_horizon)
                   .exclude(status='Canceled')
                   .values('date_time')
                   .order_by('date_time'))
    counts_by_day = {}
    for row in next7_raw:
        d = timezone.localtime(row['date_time']).date()
        counts_by_day[d] = counts_by_day.get(d, 0) + 1
    next7_days = []
    for i in range(7):
        d = (today_start + timedelta(days=i)).date()
        next7_days.append({
            'date': d,
            'is_today': i == 0,
            'count': counts_by_day.get(d, 0),
            'is_off': DoctorTimeOff.objects.filter(doctor=me, date=d).exists(),
        })

    return render(request, 'hospital/doctor/dashboard.html', {
        'me':              me,
        'today':           today_qs,
        'today_completed': completed_today_count,
        'today_upcoming':  upcoming_today_count,
        'today_in_progress': in_progress_count,
        'today_total_patients': unique_patients_today,
        'upcoming':        upcoming,
        'recent':          recent,
        'total':           total,
        'unique_patients': unique_patients,
        'next7_days':      next7_days,
        'now':             now,
    })


# ── APPOINTMENTS ─────────────────────────────────────────────


@doctor_required
def doctor_appointments(request):
    me = request.user.doctor
    status = (request.GET.get('status') or '').strip()
    qs = (me.appointments
            .select_related('patient', 'doctor__department')
            .prefetch_related('predictions')
            .order_by('-date_time'))
    if status:
        qs = qs.filter(status=status)
    return render(request, 'hospital/doctor/appointments.html', {
        'appointments': qs,
        'status':       status,
        'now':          timezone.now(),
    })


class VisitNoteForm(forms.ModelForm):
    class Meta:
        model = VisitNote
        fields = ['note', 'prescription', 'follow_up_needed', 'follow_up_date']
        widgets = {
            'note':         forms.Textarea(attrs={'rows': 4,
                                                  'placeholder': 'What did you observe? Examination findings, vitals, plan…'}),
            'prescription': forms.Textarea(attrs={'rows': 3,
                                                  'placeholder': 'e.g. Amoxicillin 500mg, 1 cap TID, 7 days'}),
            'follow_up_date': forms.DateInput(attrs={'type': 'date', 'class': 'input--lg'}),
        }


@doctor_required
def doctor_appointment_detail(request, pk):
    me = request.user.doctor
    appt = get_object_or_404(
        Appointment.objects
            .select_related('patient', 'doctor__department')
            .prefetch_related('predictions')
            .filter(doctor=me),
        pk=pk,
    )
    note = getattr(appt, 'visit_note', None)
    if request.method == 'POST' and request.POST.get('form_kind') == 'note':
        form = VisitNoteForm(request.POST, instance=note)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.appointment = appt
            obj.created_by  = request.user
            obj.save()
            messages.success(request, 'Visit note saved.')
            return redirect('doctor_appointment_detail', pk=appt.pk)
    else:
        form = VisitNoteForm(instance=note)
    return render(request, 'hospital/doctor/appointment_detail.html', {
        'appt':        appt,
        'predictions': appt.predictions.order_by('-created_at'),
        'visit_note':  note,
        'note_form':   form,
    })


@doctor_required
def doctor_appointment_reschedule(request, pk):
    """Doctor moves one of their own appointments to a new open slot."""
    from .booking import available_slots, next_available_days
    me = request.user.doctor
    appt = get_object_or_404(Appointment.objects.filter(doctor=me), pk=pk)
    if appt.status == 'Canceled':
        messages.error(request, "Canceled appointments can't be rescheduled.")
        return redirect('doctor_appointment_detail', pk=appt.pk)

    raw_date = request.GET.get('date') or ''
    try:
        target_date = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        target_date = None
    if target_date is None:
        days = next_available_days(me, n=14)
        target_date = days[0] if days else timezone.localdate()

    slots = available_slots(me, target_date)
    # Doctor is allowed to keep the current slot in the picker, so include it
    if appt.date_time.date() == target_date and appt.date_time not in slots:
        slots = [appt.date_time] + slots
        slots = sorted(slots)

    if request.method == 'POST':
        slot_iso = (request.POST.get('slot') or '').strip()
        try:
            slot_dt = datetime.fromisoformat(slot_iso)
        except ValueError:
            messages.error(request, 'Pick a valid time slot.')
            return redirect('doctor_appointment_reschedule', pk=appt.pk)
        if timezone.is_naive(slot_dt):
            slot_dt = timezone.make_aware(slot_dt, timezone.get_current_timezone())
        # Allow keeping the existing slot, otherwise validate
        if slot_dt != appt.date_time and slot_dt not in available_slots(me, slot_dt.date()):
            messages.error(request, 'That slot is taken or outside your hours.')
            return redirect('doctor_appointment_reschedule', pk=appt.pk)
        old = appt.date_time
        appt.date_time = slot_dt
        appt.save(update_fields=['date_time'])
        messages.success(
            request,
            f'Moved from {timezone.localtime(old).strftime("%b %d, %I:%M %p")} '
            f'to {slot_dt.strftime("%b %d, %I:%M %p")}.',
        )
        return redirect('doctor_appointment_detail', pk=appt.pk)

    return render(request, 'hospital/doctor/reschedule.html', {
        'appt':        appt,
        'target_date': target_date,
        'slots':       slots,
        'next_days':   next_available_days(me, n=14),
    })


@doctor_required
@require_POST
def doctor_appointment_set_status(request, pk):
    me = request.user.doctor
    appt = get_object_or_404(Appointment.objects.filter(doctor=me), pk=pk)
    new_status = (request.POST.get('status') or '').strip()
    if new_status in {'Scheduled', 'Completed', 'Canceled'}:
        appt.status = new_status
        appt.save(update_fields=['status'])
        messages.success(request, f'Appointment #{appt.pk} marked {new_status}.')
    else:
        messages.error(request, 'Invalid status.')
    return redirect('doctor_appointment_detail', pk=appt.pk)


# ── AVAILABILITY ─────────────────────────────────────────────


class AvailabilityForm(forms.ModelForm):
    class Meta:
        model = DoctorAvailability
        fields = ['weekday', 'start_time', 'end_time', 'slot_minutes']
        widgets = {
            'start_time': forms.TimeInput(attrs={'type': 'time'}),
            'end_time':   forms.TimeInput(attrs={'type': 'time'}),
        }


class TimeOffForm(forms.ModelForm):
    class Meta:
        model = DoctorTimeOff
        fields = ['date', 'reason']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date', 'class': 'input--lg'}),
            'reason': forms.TextInput(attrs={
                'placeholder': 'Optional (e.g. conference, on call)'
            }),
        }


@doctor_required
def doctor_availability(request):
    me = request.user.doctor
    form = AvailabilityForm(request.POST or None) if request.POST.get('form_kind') == 'avail' else AvailabilityForm()
    timeoff_form = TimeOffForm(request.POST or None) if request.POST.get('form_kind') == 'off' else TimeOffForm()

    if request.method == 'POST':
        kind = request.POST.get('form_kind')
        if kind == 'avail' and form.is_valid():
            obj = form.save(commit=False)
            obj.doctor = me
            try:
                obj.save()
                messages.success(request, 'Availability window added.')
                return redirect('doctor_availability')
            except Exception as exc:
                messages.error(request, f'Could not save window: {exc}')
        elif kind == 'off' and timeoff_form.is_valid():
            obj = timeoff_form.save(commit=False)
            obj.doctor = me
            try:
                obj.save()
                messages.success(request, f'Marked {obj.date:%b %d, %Y} as day off.')
                return redirect('doctor_availability')
            except Exception as exc:
                messages.error(request, f'Could not mark day off: {exc}')

    windows = me.availabilities.order_by('weekday', 'start_time')
    today = timezone.localdate()
    time_off = me.time_off.filter(date__gte=today).order_by('date')
    return render(request, 'hospital/doctor/availability.html', {
        'windows':      windows,
        'time_off':     time_off,
        'form':         form,
        'timeoff_form': timeoff_form,
    })


@doctor_required
@require_POST
def doctor_availability_delete(request, pk):
    me = request.user.doctor
    win = get_object_or_404(DoctorAvailability.objects.filter(doctor=me), pk=pk)
    win.delete()
    messages.success(request, 'Window removed.')
    return redirect('doctor_availability')


@doctor_required
@require_POST
def doctor_timeoff_delete(request, pk):
    me = request.user.doctor
    off = get_object_or_404(DoctorTimeOff.objects.filter(doctor=me), pk=pk)
    d = off.date
    off.delete()
    messages.success(request, f'Day off for {d:%b %d, %Y} removed — bookings re-open.')
    return redirect('doctor_availability')


# ── PROFILE ──────────────────────────────────────────────────


class DoctorProfileForm(forms.ModelForm):
    class Meta:
        model = Doctor
        fields = ['name', 'specialty', 'email']


@doctor_required
def doctor_profile(request):
    me = request.user.doctor
    form = DoctorProfileForm(request.POST or None, instance=me)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Profile updated.')
        return redirect('doctor_profile')
    return render(request, 'hospital/doctor/profile.html', {'form': form, 'me': me})
