import calendar as _calendar
import csv
import json
from datetime import date, datetime, time, timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required
from .permissions import staff_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.db.models.functions import TruncMonth
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from .models import AIPrediction, Appointment, AuditEvent, Department, Doctor, Patient
from .forms import DepartmentForm, DoctorForm, PatientForm, AppointmentForm
from .ai_service import predict_diagnosis

PAGE_SIZE = 10

RANGE_CHOICES = [
    ('today', 'Today'),
    ('7d',    'Last 7 days'),
    ('30d',   'Last 30 days'),
    ('all',   'All time'),
]


def _range_window(key: str):
    """Return (start_dt, end_dt, label) for a range key. None means open-ended."""
    now = timezone.localtime()
    if key == 'today':
        start = timezone.make_aware(datetime.combine(now.date(), time.min))
        return start, None, 'Today'
    if key == '7d':
        return now - timedelta(days=7), None, 'Last 7 days'
    if key == 'all':
        return None, None, 'All time'
    return now - timedelta(days=30), None, 'Last 30 days'



def home_router(request):
    """Single entry that routes by role: anon → /login/, staff → /dashboard/,
    doctor → /doctor/, patient → /patient/.

    Defensively logs out users that are authenticated but in a 'limbo' state
    (non-staff, no linked Doctor, no linked Patient) so they don't loop.
    """
    from django.contrib.auth import logout as auth_logout
    u = request.user
    if not u.is_authenticated:
        return redirect('login')
    if u.is_staff:
        return redirect('dashboard')
    if hasattr(u, 'doctor') and u.doctor is not None:
        return redirect('doctor_home')
    if hasattr(u, 'patient') and u.patient is not None:
        return redirect('patient_home')
    auth_logout(request)
    messages.error(
        request,
        'Your account exists but is not linked to a doctor or patient profile. '
        'Please contact an administrator.',
    )
    return redirect('login')


def healthz(request):
    """Lightweight readiness probe: DB ping + channel-layer reachable."""
    from django.db import connection
    payload = {'ok': True, 'db': False, 'channels': False}
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        payload['db'] = True
    except Exception:
        payload['ok'] = False
    try:
        from channels.layers import get_channel_layer
        payload['channels'] = get_channel_layer() is not None
    except Exception:
        payload['channels'] = False
    return JsonResponse(payload, status=200 if payload['ok'] else 500)


def landing(request):
    """Public landing page. Redirects authenticated users to their portal."""
    if request.user.is_authenticated:
        return redirect('dashboard' if request.user.is_staff else 'patient_home')
    return render(request, 'hospital/landing.html', {
        'stats_demo': {
            'patients':      Patient.objects.count() or 1284,
            'doctors':       Doctor.objects.count() or 42,
            'appointments':  Appointment.objects.count() or 312,
            'avg_confidence': 84,
        },
    })


@staff_required
def dashboard(request):
    from django.db.models import Avg

    range_key = request.GET.get('range', '30d')
    if range_key not in {k for k, _ in RANGE_CHOICES}:
        range_key = '30d'
    start, end, range_label = _range_window(range_key)

    appts = Appointment.objects.all()
    preds = AIPrediction.objects.filter(status='SUCCESS')
    if start:
        appts = appts.filter(date_time__gte=start)
        preds = preds.filter(appointment__date_time__gte=start)
    if end:
        appts = appts.filter(date_time__lte=end)
        preds = preds.filter(appointment__date_time__lte=end)

    # Appointment status breakdown
    status_qs = appts.values('status').annotate(count=Count('id'))
    status_map = {s['status']: s['count'] for s in status_qs}

    # Appointments per department
    dept_qs = (appts
               .values('doctor__department__name')
               .annotate(count=Count('id'))
               .order_by('-count')[:7])
    dept_labels = [d['doctor__department__name'] or 'Unknown' for d in dept_qs]
    dept_counts = [d['count'] for d in dept_qs]

    # Monthly trend (last 6 months — independent of range to keep history visible)
    monthly_qs = (Appointment.objects
                  .annotate(month=TruncMonth('date_time'))
                  .values('month')
                  .annotate(count=Count('id'))
                  .order_by('month')[:6])
    month_labels = [m['month'].strftime('%b %Y') if m['month'] else '' for m in monthly_qs]
    month_counts = [m['count'] for m in monthly_qs]

    # Top doctors by appointment count (within range)
    doctor_qs = (appts
                 .values('doctor__name')
                 .annotate(count=Count('id'))
                 .order_by('-count')[:7])
    doctor_labels = [d['doctor__name'] for d in doctor_qs]
    doctor_counts = [d['count'] for d in doctor_qs]

    # AI coverage (within range)
    total_appts = appts.count()
    analyzed = appts.filter(predictions__status='SUCCESS').distinct().count()
    ai_coverage = int(analyzed / total_appts * 100) if total_appts else 0

    # Average confidence score (within range)
    avg_conf = preds.aggregate(avg=Avg('confidence_score'))['avg'] or 0
    avg_conf_pct = int(avg_conf * 100)

    recent_qs = (appts.select_related('doctor', 'patient')
                      .order_by('-date_time')[:5])

    recent_audit = AuditEvent.objects.select_related('user').order_by('-created_at')[:8]

    context = {
        'stats': {
            'departments':    Department.objects.count(),
            'doctors':        Doctor.objects.count(),
            'patients':       Patient.objects.count(),
            'appointments':   total_appts,
            'ai_coverage':    ai_coverage,
            'avg_confidence': avg_conf_pct,
        },
        'recent_appointments': recent_qs,
        'recent_audit':  recent_audit,
        'range_key':     range_key,
        'range_label':   range_label,
        'range_choices': RANGE_CHOICES,
        'dept_count':    Department.objects.count(),
        'doctor_count':  Doctor.objects.count(),
        'patient_count': Patient.objects.count(),
        'appt_count':    total_appts,
        'ai_coverage':   ai_coverage,
        'avg_conf_pct':  avg_conf_pct,
        'recent_appts':  recent_qs,
        'status_data':   json.dumps([status_map.get('Scheduled', 0), status_map.get('Completed', 0), status_map.get('Canceled', 0)]),
        'dept_labels':   json.dumps(dept_labels),
        'dept_counts':   json.dumps(dept_counts),
        'month_labels':  json.dumps(month_labels),
        'month_counts':  json.dumps(month_counts),
        'doctor_labels': json.dumps(doctor_labels),
        'doctor_counts': json.dumps(doctor_counts),
    }
    return render(request, 'hospital/dashboard.html', context)


# ── DEPARTMENTS ────────────────────────────────────────────────


@staff_required
def departments(request):
    q = request.GET.get('q', '').strip()
    qs = Department.objects.annotate(doctor_count=Count('doctors')).order_by('name')
    if q:
        qs = qs.filter(name__icontains=q)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    return render(request, 'hospital/departments.html', {
        'departments': page, 'q': q,
        'page_query': f'&q={q}' if q else '',
    })


@staff_required
def department_detail(request, pk):
    """Per-department deep dive: doctors, appointment volume, top diagnoses,
    average AI confidence, average patient rating."""
    from django.db.models import Avg, FloatField
    from django.db.models.functions import Cast, TruncMonth

    dept = get_object_or_404(Department, pk=pk)

    doctors = (dept.doctors
                   .select_related('department')
                   .annotate(num_appts=Count('appointments', distinct=True))
                   .annotate(num_patients=Count('appointments__patient', distinct=True))
                   .annotate(avg_rating=Avg('appointments__rating__stars'))
                   .order_by('-num_appts', 'name'))

    appts_qs = Appointment.objects.filter(doctor__department=dept)
    total_appts = appts_qs.count()
    completed   = appts_qs.filter(status='Completed').count()
    scheduled   = appts_qs.filter(status='Scheduled').count()
    canceled    = appts_qs.filter(status='Canceled').count()
    unique_patients = appts_qs.values('patient').distinct().count()

    # Today's count for this department
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = appts_qs.filter(date_time__gte=today_start,
                                  date_time__lt=today_start + timedelta(days=1)).count()

    # AI coverage + average confidence
    pred_qs = AIPrediction.objects.filter(appointment__doctor__department=dept, status='SUCCESS')
    avg_conf = pred_qs.aggregate(a=Avg('confidence_score'))['a'] or 0
    analyzed = appts_qs.filter(predictions__status='SUCCESS').distinct().count()
    coverage = int(analyzed / total_appts * 100) if total_appts else 0

    # Top 5 diagnoses for the department (from successful AI predictions)
    top_dx = (pred_qs.values('predicted_diagnosis')
                     .annotate(n=Count('id'))
                     .order_by('-n')[:5])

    # Average patient rating across the department
    from .models import AppointmentRating
    avg_rating = (AppointmentRating.objects
                   .filter(appointment__doctor__department=dept)
                   .aggregate(a=Avg('stars'))['a']) or 0
    rating_count = (AppointmentRating.objects
                     .filter(appointment__doctor__department=dept).count())

    # Monthly volume (last 6 months) — for a sparkline if we want it
    six_months_ago = timezone.now() - timedelta(days=183)
    monthly = (appts_qs.filter(date_time__gte=six_months_ago)
                       .annotate(m=TruncMonth('date_time'))
                       .values('m')
                       .annotate(n=Count('id'))
                       .order_by('m'))

    return render(request, 'hospital/department_detail.html', {
        'department':     dept,
        'doctors':        doctors,
        'total_appts':    total_appts,
        'completed':      completed,
        'scheduled':      scheduled,
        'canceled':       canceled,
        'today_count':    today_count,
        'unique_patients': unique_patients,
        'avg_conf_pct':   int(avg_conf * 100),
        'coverage':       coverage,
        'analyzed':       analyzed,
        'top_dx':         top_dx,
        'avg_rating':     round(avg_rating, 2),
        'rating_count':   rating_count,
        'monthly':        list(monthly),
    })


@staff_required
def department_create(request):
    form = DepartmentForm(request.POST or None)
    if form.is_valid():
        obj = form.save()
        messages.success(request, f'Department "{obj.name}" created successfully.')
        return redirect('departments')
    return render(request, 'hospital/form.html', {'form': form, 'form_title': 'Add Department', 'cancel_url': 'departments'})


@staff_required
def department_edit(request, pk):
    dept = get_object_or_404(Department, pk=pk)
    form = DepartmentForm(request.POST or None, instance=dept)
    if form.is_valid():
        form.save()
        messages.success(request, f'Department "{dept.name}" updated.')
        return redirect('departments')
    return render(request, 'hospital/form.html', {'form': form, 'form_title': f'Edit Department — {dept.name}', 'cancel_url': 'departments'})


@staff_required
def department_delete(request, pk):
    dept = get_object_or_404(Department, pk=pk)
    if request.method == 'POST':
        name = dept.name
        try:
            dept.delete()
            messages.success(request, f'Department "{name}" deleted.')
        except ProtectedError:
            messages.error(request, f'Cannot delete "{name}" — it has doctors assigned to it.')
        return redirect('departments')
    return render(request, 'hospital/confirm_delete.html', {'object_name': dept.name, 'object_label': dept.name, 'cancel_url': 'departments'})


# ── DOCTORS ────────────────────────────────────────────────────


@staff_required
def doctors(request):
    q = request.GET.get('q', '').strip()
    qs = Doctor.objects.select_related('department').order_by('name')
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(specialty__icontains=q))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    return render(request, 'hospital/doctors.html', {
        'doctors': page, 'q': q,
        'page_query': f'&q={q}' if q else '',
    })


@staff_required
def doctor_detail(request, pk):
    from django.db.models import Avg

    doctor = get_object_or_404(Doctor.objects.select_related('department'), pk=pk)
    appts = (doctor.appointments
                   .select_related('patient')
                   .prefetch_related('predictions')
                   .order_by('-date_time'))

    status_counts = appts.values('status').annotate(count=Count('id'))
    sc = {s['status']: s['count'] for s in status_counts}

    avg_conf = (AIPrediction.objects
                .filter(appointment__doctor=doctor, status='SUCCESS')
                .aggregate(avg=Avg('confidence_score'))['avg']) or 0

    upcoming = (doctor.appointments
                      .filter(date_time__gte=timezone.now())
                      .select_related('patient')
                      .order_by('date_time')[:7])

    unique_patients = (doctor.appointments
                             .values('patient').distinct().count())

    return render(request, 'hospital/doctor_detail.html', {
        'doctor':        doctor,
        'appts':         appts[:20],
        'total_appts':   appts.count(),
        'completed':     sc.get('Completed', 0),
        'scheduled':     sc.get('Scheduled', 0),
        'canceled':      sc.get('Canceled', 0),
        'avg_conf_pct':  int(avg_conf * 100),
        'upcoming':      upcoming,
        'unique_patients': unique_patients,
    })


@staff_required
def doctor_create(request):
    form = DoctorForm(request.POST or None)
    if form.is_valid():
        obj = form.save()
        messages.success(request, f'Dr. {obj.name} added successfully.')
        return redirect('doctors')
    return render(request, 'hospital/form.html', {'form': form, 'form_title': 'Add Doctor', 'cancel_url': 'doctors'})


@staff_required
def doctor_edit(request, pk):
    doctor = get_object_or_404(Doctor, pk=pk)
    form = DoctorForm(request.POST or None, instance=doctor)
    if form.is_valid():
        form.save()
        messages.success(request, f'Dr. {doctor.name} updated.')
        return redirect('doctors')
    return render(request, 'hospital/form.html', {'form': form, 'form_title': f'Edit Doctor — {doctor.name}', 'cancel_url': 'doctors'})


@staff_required
def doctor_delete(request, pk):
    doctor = get_object_or_404(Doctor, pk=pk)
    if request.method == 'POST':
        name = doctor.name
        try:
            doctor.delete()
            messages.success(request, f'Dr. {name} deleted.')
        except ProtectedError:
            messages.error(request, f'Cannot delete Dr. {name} — they have appointments. Delete the appointments first.')
        return redirect('doctors')
    return render(request, 'hospital/confirm_delete.html', {'object_name': str(doctor), 'object_label': str(doctor), 'cancel_url': 'doctors'})


# ── PATIENTS ───────────────────────────────────────────────────


@staff_required
def patients(request):
    q = request.GET.get('q', '').strip()
    qs = Patient.objects.annotate(appointment_count=Count('appointments')).order_by('name')
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    return render(request, 'hospital/patients.html', {
        'patients': page, 'q': q,
        'page_query': f'&q={q}' if q else '',
    })


@staff_required
def patient_detail(request, pk):
    from django.db.models import Avg

    patient = get_object_or_404(Patient, pk=pk)
    appts = (patient.appointments
                    .select_related('doctor', 'doctor__department')
                    .prefetch_related('predictions')
                    .order_by('-date_time'))

    status_counts = appts.values('status').annotate(count=Count('id'))
    status_map = {s['status']: s['count'] for s in status_counts}

    avg_conf = (AIPrediction.objects
                .filter(appointment__patient=patient, status='SUCCESS')
                .aggregate(avg=Avg('confidence_score'))['avg']) or 0

    today = timezone.localdate()
    age = None
    if patient.date_of_birth:
        dob = patient.date_of_birth
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    next_appt = (patient.appointments
                       .filter(date_time__gte=timezone.now(), status='Scheduled')
                       .order_by('date_time').first())

    return render(request, 'hospital/patient_detail.html', {
        'patient':     patient,
        'age':         age,
        'appts':       appts,
        'total_appts': appts.count(),
        'completed':   status_map.get('Completed', 0),
        'scheduled':   status_map.get('Scheduled', 0),
        'canceled':    status_map.get('Canceled',  0),
        'avg_conf_pct': int(avg_conf * 100),
        'last_visit':  appts.filter(status='Completed').first(),
        'next_appt':   next_appt,
    })


@staff_required
def patient_create(request):
    form = PatientForm(request.POST or None)
    if form.is_valid():
        obj = form.save()
        messages.success(request, f'Patient "{obj.name}" registered.')
        return redirect('patients')
    return render(request, 'hospital/form.html', {'form': form, 'form_title': 'Add Patient', 'cancel_url': 'patients'})


@staff_required
def patient_edit(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    form = PatientForm(request.POST or None, instance=patient)
    if form.is_valid():
        form.save()
        messages.success(request, f'Patient "{patient.name}" updated.')
        return redirect('patients')
    return render(request, 'hospital/form.html', {'form': form, 'form_title': f'Edit Patient — {patient.name}', 'cancel_url': 'patients'})


@staff_required
def patient_delete(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    if request.method == 'POST':
        name = patient.name
        try:
            patient.delete()
            messages.success(request, f'Patient "{name}" deleted.')
        except ProtectedError:
            messages.error(request, f'Cannot delete "{name}" — they have appointments. Delete the appointments first.')
        return redirect('patients')
    return render(request, 'hospital/confirm_delete.html', {'object_name': patient.name, 'object_label': patient.name, 'cancel_url': 'patients'})


# ── APPOINTMENTS ───────────────────────────────────────────────


@staff_required
def appointments(request):
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    qs = Appointment.objects.select_related('doctor', 'doctor__department', 'patient').order_by('-date_time')
    if q:
        qs = qs.filter(Q(patient__name__icontains=q) | Q(doctor__name__icontains=q) | Q(reason__icontains=q))
    if status:
        qs = qs.filter(status=status)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get('page'))
    page_query = ('&q=' + q if q else '') + ('&status=' + status if status else '')
    return render(request, 'hospital/appointments.html', {
        'appointments': page, 'q': q, 'status_filter': status,
        'page_query': page_query,
    })


@staff_required
def appointments_calendar(request):
    today = timezone.localdate()
    try:
        year  = int(request.GET.get('year',  today.year))
        month = int(request.GET.get('month', today.month))
    except (TypeError, ValueError):
        year, month = today.year, today.month
    if not (1 <= month <= 12):
        month = today.month

    cal = _calendar.Calendar(firstweekday=6)  # Sunday-first
    weeks = cal.monthdatescalendar(year, month)
    first_day = weeks[0][0]
    last_day  = weeks[-1][-1]

    start_dt = timezone.make_aware(datetime.combine(first_day, time.min))
    end_dt   = timezone.make_aware(datetime.combine(last_day,  time.max))

    qs = (Appointment.objects
          .select_related('doctor', 'patient')
          .filter(date_time__gte=start_dt, date_time__lte=end_dt)
          .order_by('date_time'))

    by_day: dict[date, list] = {}
    for appt in qs:
        local = timezone.localtime(appt.date_time)
        by_day.setdefault(local.date(), []).append(appt)

    cells = []
    for week in weeks:
        for day in week:
            items = by_day.get(day, [])
            cells.append({
                'date':       day,
                'in_month':   day.month == month,
                'is_today':   day == today,
                'count':      len(items),
                'preview':    items[:3],
                'overflow':   max(0, len(items) - 3),
            })

    if month == 1:
        prev_y, prev_m = year - 1, 12
    else:
        prev_y, prev_m = year, month - 1
    if month == 12:
        next_y, next_m = year + 1, 1
    else:
        next_y, next_m = year, month + 1

    return render(request, 'hospital/appointments_calendar.html', {
        'cells':       cells,
        'year':        year,
        'month':       month,
        'month_label': date(year, month, 1).strftime('%B %Y'),
        'today':       today,
        'prev_y':      prev_y, 'prev_m': prev_m,
        'next_y':      next_y, 'next_m': next_m,
        'weekday_labels': ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
        'total_in_month': sum(c['count'] for c in cells if c['in_month']),
    })


@staff_required
def appointment_create(request):
    initial = {}
    patient_pre = request.GET.get('patient')
    if patient_pre and patient_pre.isdigit() and Patient.objects.filter(pk=patient_pre).exists():
        initial['patient'] = patient_pre
    doctor_pre = request.GET.get('doctor')
    if doctor_pre and doctor_pre.isdigit() and Doctor.objects.filter(pk=doctor_pre).exists():
        initial['doctor'] = doctor_pre
    form = AppointmentForm(request.POST or None, initial=initial)
    if form.is_valid():
        appt = form.save(commit=False)
        appt.save()

        try:
            result = predict_diagnosis(appt)
        except Exception as e:
            result = {
                "predicted_diagnosis": "",
                "confidence_score": 0.0,
                "model_version": "unknown",
                "status": "FAILED",
                "error_message": str(e),
                "latency_ms": 0,
                "total_tokens": 0,
            }

        AIPrediction.objects.create(appointment=appt, **result)

        if result['status'] == 'SUCCESS':
            messages.success(
                request,
                f'Appointment scheduled. AI diagnosis: {result["predicted_diagnosis"]} '
                f'(confidence {int(result["confidence_score"] * 100)}%, {result["total_tokens"]} tokens).'
            )
        else:
            messages.warning(
                request,
                f'Appointment scheduled, but AI diagnosis failed: {result["error_message"]}'
            )
        return redirect('dashboard')
    return render(request, 'hospital/form.html', {'form': form, 'form_title': 'Add Appointment', 'cancel_url': 'appointments'})


@staff_required
def appointment_edit(request, pk):
    appt = get_object_or_404(Appointment, pk=pk)
    form = AppointmentForm(request.POST or None, instance=appt)
    if form.is_valid():
        form.save()
        messages.success(request, f'Appointment #{appt.pk} updated.')
        return redirect('appointments')
    return render(request, 'hospital/form.html', {'form': form, 'form_title': f'Edit Appointment #{appt.pk}', 'cancel_url': 'appointments'})


@staff_required
def appointment_delete(request, pk):
    appt = get_object_or_404(Appointment, pk=pk)
    if request.method == 'POST':
        appt.delete()
        messages.success(request, f'Appointment #{pk} deleted.')
        return redirect('appointments')
    return render(request, 'hospital/confirm_delete.html', {'object_name': str(appt), 'object_label': str(appt), 'cancel_url': 'appointments'})


# ── AI PREDICTION ──────────────────────────────────────────────


@staff_required
def appointment_predict(request, pk):
    """Run a fresh AI diagnosis for an appointment.

    By default we return to the page the user came from (HTTP_REFERER) so
    "Run analysis" buttons on the predictions page, appointment detail page,
    and patient detail page all stay put. Falls back to the per-appointment
    predictions page (never the bulk list, which loses scroll position).
    """
    appt = get_object_or_404(Appointment, pk=pk)
    result = predict_diagnosis(appt)
    AIPrediction.objects.create(appointment=appt, **result)
    if result['status'] == 'SUCCESS':
        messages.success(request, f'AI diagnosis complete: {result["predicted_diagnosis"]}')
    else:
        messages.error(request, f'AI diagnosis failed: {result["error_message"]}')

    next_url = (request.POST.get('next')
                or request.GET.get('next')
                or request.META.get('HTTP_REFERER'))
    if next_url and next_url.startswith('/') and '\n' not in next_url:
        return redirect(next_url)
    return redirect('appointment_predictions', pk=appt.pk)


# ── AI PREDICTION HISTORY ─────────────────────────────────────


@staff_required
def appointment_predictions(request, pk):
    appt = get_object_or_404(
        Appointment.objects.select_related('patient', 'doctor', 'doctor__department'), pk=pk
    )
    predictions = appt.predictions.order_by('-created_at')
    return render(request, 'hospital/predictions.html', {
        'appt': appt,
        'appointment': appt,
        'predictions': predictions,
    })


# ── BULK ACTIONS ───────────────────────────────────────────────


@staff_required
@require_POST
def appointments_bulk_action(request):
    """Apply a bulk action to selected appointments. Action ∈ {cancel, complete, analyze}."""
    action = (request.POST.get('action') or '').strip().lower()
    raw_ids = request.POST.get('ids') or ''
    ids = [int(x) for x in raw_ids.split(',') if x.isdigit()]
    if not ids:
        messages.warning(request, 'No appointments selected.')
        return redirect('appointments')
    qs = Appointment.objects.filter(pk__in=ids)
    n = qs.count()
    if n == 0:
        messages.warning(request, 'No matching appointments found.')
        return redirect('appointments')

    if action == 'cancel':
        qs.update(status='Canceled')
        messages.success(request, f'Canceled {n} appointment{"s" if n != 1 else ""}.')
    elif action == 'complete':
        qs.update(status='Completed')
        messages.success(request, f'Marked {n} appointment{"s" if n != 1 else ""} completed.')
    elif action == 'analyze':
        ok, fail = 0, 0
        for appt in qs.select_related('patient', 'doctor', 'doctor__department'):
            try:
                result = predict_diagnosis(appt)
            except Exception as exc:
                result = {'predicted_diagnosis': '', 'confidence_score': 0.0, 'model_version': 'unknown',
                          'status': 'FAILED', 'error_message': str(exc), 'latency_ms': 0, 'total_tokens': 0}
            AIPrediction.objects.create(appointment=appt, **result)
            if result['status'] == 'SUCCESS':
                ok += 1
            else:
                fail += 1
        msg = f'Analyzed {ok}/{n} appointments'
        if fail:
            msg += f' ({fail} failed)'
        messages.success(request, msg + '.')
    else:
        messages.error(request, f'Unknown bulk action: {action!r}')
    return redirect('appointments')


# ── BULK ANALYZE ───────────────────────────────────────────────


@staff_required
def bulk_predict(request):
    if request.method != 'POST':
        return redirect('appointments')

    unanalyzed = Appointment.objects.select_related(
        'patient', 'doctor', 'doctor__department'
    ).exclude(predictions__status='SUCCESS')

    total = unanalyzed.count()
    if total == 0:
        messages.success(request, 'All appointments already have a successful AI diagnosis.')
        return redirect('appointments')

    success_count = 0
    for appt in unanalyzed:
        result = predict_diagnosis(appt)
        AIPrediction.objects.create(appointment=appt, **result)
        if result['status'] == 'SUCCESS':
            success_count += 1

    failed = total - success_count
    messages.success(request, f'Bulk analysis complete: {success_count}/{total} succeeded'
                               + (f', {failed} failed.' if failed else '.'))
    return redirect('appointments')


# ── CSV EXPORTS ────────────────────────────────────────────────


@staff_required
def export_appointments_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="appointments.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Patient', 'Doctor', 'Department', 'Date & Time', 'Reason', 'Status'])
    for appt in Appointment.objects.select_related('patient', 'doctor', 'doctor__department').order_by('-date_time'):
        writer.writerow([appt.pk, appt.patient.name, appt.doctor.name, appt.doctor.department.name,
                         appt.date_time.strftime('%Y-%m-%d %H:%M'), appt.reason, appt.status])
    return response


@staff_required
def export_patients_csv(request):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="patients.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', 'Name', 'Date of Birth', 'Phone', 'Total Appointments'])
    for pat in Patient.objects.prefetch_related('appointments').all():
        writer.writerow([pat.pk, pat.name, pat.date_of_birth, pat.phone, pat.appointments.count()])
    return response


# ── AUTH ───────────────────────────────────────────────────────

def register(request):
    """Legacy generic register — public signups go through /patient/register/ now.

    Sends visitors to the patient flow (so they end up with a proper Patient row).
    Staff users should be added through /admin/auth/user/ by an admin.
    """
    messages.info(request, 'Public sign-ups now create a patient account.')
    return redirect('patient_register')


# ── AI INSIGHT (dashboard) ──────────────────────────────────────


@staff_required
def dashboard_ai_insight(request):
    """Generate a one-paragraph LLM summary of today's notable cases."""
    from django.db.models import Avg
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    from django.conf import settings as dj_settings

    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    today_qs = Appointment.objects.filter(date_time__gte=today_start, date_time__lt=today_end)
    low_conf = AIPrediction.objects.filter(status='SUCCESS', confidence_score__lt=0.5).count()
    avg_conf = AIPrediction.objects.filter(status='SUCCESS').aggregate(avg=Avg('confidence_score'))['avg'] or 0
    weekly = Appointment.objects.filter(date_time__gte=today_start - timedelta(days=7)).count()

    summary_facts = (
        f"Today: {today_qs.count()} appointments "
        f"({today_qs.filter(status='Scheduled').count()} scheduled, "
        f"{today_qs.filter(status='Completed').count()} completed, "
        f"{today_qs.filter(status='Canceled').count()} canceled). "
        f"Past 7 days: {weekly} appointments. "
        f"Low-confidence AI flags requiring review: {low_conf}. "
        f"Average AI confidence: {int(avg_conf * 100)}%. "
        f"Total patients: {Patient.objects.count()}, doctors: {Doctor.objects.count()}."
    )

    insight = None
    error = None
    try:
        llm = ChatOpenAI(
            model="lightning-ai/deepseek-v4-pro",
            api_key=dj_settings.LIGHTNING_API_KEY,
            base_url="https://lightning.ai/api/v1/",
            temperature=0.5,
            max_tokens=140,
        )
        prompt = (
            "You are a clinic operations assistant. Given the facts below, write "
            "a concise 2-3 sentence executive insight for the clinical lead. "
            "Highlight what's notable, what needs attention, and end with a brief "
            "recommendation. Do NOT list the numbers verbatim — interpret them.\n\n"
            f"Facts: {summary_facts}"
        )
        resp = llm.invoke([HumanMessage(content=prompt)])
        insight = (resp.content if isinstance(resp.content, str) else str(resp.content)).strip()
    except Exception as exc:
        error = str(exc)

    return render(request, 'hospital/_ai_insight.html', {
        'insight': insight,
        'error':   error,
        'facts':   summary_facts,
        'now':     timezone.localtime(),
    })


# ── NOTIFICATIONS ──────────────────────────────────────────────


def _build_notifications_staff():
    """Compute the staff notifications list — low-confidence AI flags + upcoming appointments."""
    now = timezone.now()
    horizon = now + timedelta(hours=24)

    low_conf = (AIPrediction.objects
                .filter(status='SUCCESS', confidence_score__lt=0.5)
                .select_related('appointment', 'appointment__patient', 'appointment__doctor')
                .order_by('-created_at')[:5])

    upcoming = (Appointment.objects
                .filter(date_time__gte=now, date_time__lte=horizon, status='Scheduled')
                .select_related('patient', 'doctor')
                .order_by('date_time')[:5])

    items = []
    for p in low_conf:
        items.append({
            'kind':  'low_conf',
            'when':  p.created_at,
            'title': f'Low confidence: {p.predicted_diagnosis[:60]}',
            'sub':   f'{p.appointment.patient.name} · Dr. {p.appointment.doctor.name} · {int(p.confidence_score*100)}%',
            'url':   f'/appointments/{p.appointment.pk}/predictions/',
        })
    for a in upcoming:
        local = timezone.localtime(a.date_time)
        items.append({
            'kind':  'upcoming',
            'when':  a.date_time,
            'title': f'Upcoming: {a.patient.name}',
            'sub':   f'{local:%b %d, %I:%M %p} · Dr. {a.doctor.name} · {a.reason[:50]}',
            'url':   f'/appointments/{a.pk}/predictions/',
        })
    items.sort(key=lambda x: x['when'], reverse=True)
    return items[:8]


def _build_notifications_patient(patient):
    """Patient-scoped notifications: their upcoming visits, new doctor notes, pending ratings."""
    from .models import VisitNote, AppointmentRating
    now = timezone.now()
    horizon = now + timedelta(hours=48)
    items = []

    upcoming = (Appointment.objects
                .filter(patient=patient, status='Scheduled',
                        date_time__gte=now, date_time__lte=horizon)
                .select_related('doctor')
                .order_by('date_time')[:5])
    for a in upcoming:
        local = timezone.localtime(a.date_time)
        items.append({
            'kind':  'upcoming',
            'when':  a.date_time,
            'title': f'Upcoming visit · {local:%a %b %d, %I:%M %p}',
            'sub':   f'Dr. {a.doctor.name} · {a.reason[:60] or "—"}',
            'url':   f'/patient/appointments/{a.pk}/',
        })

    recent_notes = (VisitNote.objects
                    .filter(appointment__patient=patient)
                    .select_related('appointment', 'appointment__doctor')
                    .order_by('-updated_at')[:5])
    for n in recent_notes:
        items.append({
            'kind':  'low_conf',
            'when':  n.updated_at,
            'title': f"Doctor's notes added",
            'sub':   f'Dr. {n.appointment.doctor.name} · {(n.note or n.prescription or "")[:60]}',
            'url':   f'/patient/appointments/{n.appointment.pk}/',
        })

    rated_pks = set(AppointmentRating.objects
                    .filter(appointment__patient=patient)
                    .values_list('appointment_id', flat=True))
    pending = (Appointment.objects
               .filter(patient=patient, status='Completed')
               .exclude(pk__in=rated_pks)
               .select_related('doctor')
               .order_by('-date_time')[:5])
    for a in pending:
        items.append({
            'kind':  'upcoming',
            'when':  a.date_time,
            'title': 'Rate your visit',
            'sub':   f'Dr. {a.doctor.name} · {timezone.localtime(a.date_time):%b %d}',
            'url':   f'/patient/appointments/{a.pk}/rate/',
        })

    items.sort(key=lambda x: x['when'], reverse=True)
    return items[:8]


def _build_notifications_doctor(doctor):
    """Doctor-scoped notifications: today's queue, new AI flags on their patients."""
    now = timezone.now()
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    items = []

    todays = (Appointment.objects
              .filter(doctor=doctor, date_time__gte=now, date_time__lt=today_end,
                      status='Scheduled')
              .select_related('patient')
              .order_by('date_time')[:5])
    for a in todays:
        local = timezone.localtime(a.date_time)
        items.append({
            'kind':  'upcoming',
            'when':  a.date_time,
            'title': f'Today · {local:%I:%M %p} · {a.patient.name}',
            'sub':   f'{a.reason[:70] or "—"}',
            'url':   f'/doctor/appointments/{a.pk}/',
        })

    low_conf = (AIPrediction.objects
                .filter(status='SUCCESS', confidence_score__lt=0.5,
                        appointment__doctor=doctor)
                .select_related('appointment', 'appointment__patient')
                .order_by('-created_at')[:5])
    for p in low_conf:
        items.append({
            'kind':  'low_conf',
            'when':  p.created_at,
            'title': f'Low confidence: {p.predicted_diagnosis[:60]}',
            'sub':   f'{p.appointment.patient.name} · {int(p.confidence_score*100)}%',
            'url':   f'/doctor/appointments/{p.appointment.pk}/',
        })

    items.sort(key=lambda x: x['when'], reverse=True)
    return items[:8]


def _notifications_for(user):
    """Route to the right notifications builder based on the user's role."""
    if not user.is_authenticated:
        return []
    if user.is_staff:
        return _build_notifications_staff()
    if hasattr(user, 'doctor') and user.doctor is not None:
        return _build_notifications_doctor(user.doctor)
    if hasattr(user, 'patient') and user.patient is not None:
        return _build_notifications_patient(user.patient)
    return []


@login_required
def notifications(request):
    """HTMX-friendly notifications dropdown panel. Role-aware."""
    items = _notifications_for(request.user)
    return render(request, 'hospital/_notifications_panel.html', {'items': items})


@login_required
def notifications_count(request):
    """Lightweight JSON endpoint for the bell badge. Role-aware."""
    return JsonResponse({'count': len(_notifications_for(request.user))})


# ── CHATBOT ────────────────────────────────────────────────────

from .models import ChatSession, ChatMessage as _ChatMessage
from .chat_service import respond as chat_respond

_VALID_KINDS = {'general', 'dashboard', 'patient', 'appointment'}


def _serialize_message(m: _ChatMessage) -> dict:
    return {
        'id':         m.pk,
        'role':       m.role,
        'content':    m.content,
        'tool_name':  m.tool_name,
        'created_at': m.created_at.isoformat(),
    }


@login_required
@require_http_methods(['GET'])
def chat_open(request):
    """Idempotent: return the user's most recent chat session for this page context,
    or create a new one. Returns session id + visible messages (user + assistant only)."""
    kind = request.GET.get('kind', 'general')
    if kind not in _VALID_KINDS:
        kind = 'general'
    raw_id = request.GET.get('id')
    cid = int(raw_id) if (raw_id and raw_id.isdigit()) else None

    session = (ChatSession.objects
               .filter(user=request.user, page_context_kind=kind, page_context_id=cid)
               .order_by('-updated_at').first())
    if session is None:
        session = ChatSession.objects.create(
            user=request.user, page_context_kind=kind, page_context_id=cid,
        )

    visible = session.messages.exclude(role='tool').order_by('created_at')
    return JsonResponse({
        'session_id':   session.pk,
        'kind':         session.page_context_kind,
        'context_id':   session.page_context_id,
        'title':        session.title,
        'messages':     [_serialize_message(m) for m in visible],
    })


@login_required
@require_POST
def chat_send(request, session_id):
    session = get_object_or_404(ChatSession, pk=session_id, user=request.user)
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('invalid json')
    text = (payload.get('message') or '').strip()
    if not text:
        return JsonResponse({'error': 'empty message'}, status=400)
    if len(text) > 2000:
        return JsonResponse({'error': 'message too long (max 2000 chars)'}, status=400)
    try:
        result = chat_respond(session, text)
    except Exception as exc:  # pragma: no cover — surfaced to UI
        return JsonResponse({'error': str(exc)}, status=500)
    return JsonResponse({
        'reply':        result['reply'],
        'total_tokens': result['total_tokens'],
        'latency_ms':   result['latency_ms'],
        'session_id':   session.pk,
    })


@login_required
@require_POST
def chat_reset(request, session_id):
    session = get_object_or_404(ChatSession, pk=session_id, user=request.user)
    session.messages.all().delete()
    session.title = ''
    session.save()
    return JsonResponse({'ok': True, 'session_id': session.pk})
