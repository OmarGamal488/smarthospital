"""Seed the database with realistic demo data: patients, doctors, departments,
appointments spanning the past 6 months and next 2 months, plus AI predictions
on most past appointments. Idempotent on re-runs (--reset wipes first).

Usage:
    python manage.py seed_demo --patients 80 --appointments 400
    python manage.py seed_demo --reset
"""

import random
from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from hospital.models import (
    AIPrediction,
    Appointment,
    AuditEvent,
    Department,
    Doctor,
    DoctorAvailability,
    Patient,
)

DEPARTMENTS = [
    ('Cardiology',     'Heart and circulatory care'),
    ('Neurology',      'Brain, spine, and nervous system'),
    ('Pediatrics',     'Care for infants, children, and adolescents'),
    ('Radiology',      'Diagnostic imaging and interventional procedures'),
    ('Orthopedics',    'Bones, joints, and musculoskeletal'),
    ('Dermatology',    'Skin, hair, and nails'),
    ('Oncology',       'Cancer diagnosis and treatment'),
    ('General',        'General practice and primary care'),
]

DOCTOR_FIRST = [
    'Mostafa', 'Sherif', 'Heba', 'Yasmine', 'Karim', 'Tamer', 'Amira',
    'Ahmad', 'Nadia', 'Hossam', 'Reem', 'Ibrahim', 'Salma', 'Omar',
    'Layla', 'Khaled', 'Farah', 'Ziad', 'Maryam', 'Hassan', 'Dina',
    'Mohamed', 'Aya', 'Walid', 'Sara', 'Tarek', 'Heidi', 'Adel',
]
DOCTOR_LAST = [
    'Sabri', 'Khalil', 'Nour', 'Farouk', 'Eldeeb', 'Helal', 'Abdelfattah',
    'Roshdy', 'Aboulnasr', 'Bayoumi', 'Elsayed', 'Mostafa', 'Hegazy',
    'Salem', 'Anwar', 'Saadawi', 'Younes', 'Bassiouny', 'Wahba', 'Ramadan',
]

SPECIALTY_BY_DEPT = {
    'Cardiology':  ['Cardiologist', 'Interventional cardiologist', 'Electrophysiologist'],
    'Neurology':   ['Neurologist', 'Neuro-oncologist', 'Stroke specialist'],
    'Pediatrics':  ['Pediatrician', 'Neonatologist', 'Pediatric pulmonologist'],
    'Radiology':   ['Radiologist', 'Interventional radiologist', 'Diagnostic imaging'],
    'Orthopedics': ['Orthopedic surgeon', 'Sports medicine', 'Joint reconstruction'],
    'Dermatology': ['Dermatologist', 'Cosmetic dermatology', 'Pediatric dermatology'],
    'Oncology':    ['Oncologist', 'Hematologist', 'Radiation oncologist'],
    'General':     ['General practitioner', 'Internal medicine', 'Family medicine'],
}

PATIENT_FIRST_M = [
    'Yousef', 'Adam', 'Ziad', 'Khaled', 'Omar', 'Karim', 'Hassan', 'Tamer',
    'Mostafa', 'Mahmoud', 'Ahmad', 'Walid', 'Ibrahim', 'Mazen', 'Hossam',
    'Marwan', 'Sherif', 'Tarek', 'Amr', 'Bassem', 'Nader', 'Sami', 'Wael',
    'Adel', 'Fady', 'Ramy', 'Salah', 'Magdy', 'Anwar', 'Bahaa',
]
PATIENT_FIRST_F = [
    'Nour', 'Salma', 'Layla', 'Maryam', 'Farah', 'Reem', 'Dina', 'Lina',
    'Aya', 'Heba', 'Sara', 'Yasmine', 'Mona', 'Rania', 'Heidi', 'Amira',
    'Nadia', 'Yara', 'Hala', 'Eman', 'Iman', 'Safia', 'Mariam', 'Doaa',
    'Rasha', 'Soha', 'Ghada', 'Manal',
]
PATIENT_LAST = [
    'Abdelrahman', 'Tarek', 'Ibrahim', 'Mostafa', 'Hossam', 'Sherif', 'Karim',
    'Mahmoud', 'Magdy', 'Walid', 'Bassiouny', 'Amr', 'Yasser', 'Fady',
    'Salem', 'Abdelaziz', 'Helmy', 'Anwar', 'Saadawi', 'Bayoumi', 'Eissa',
    'Younes', 'Hegazy', 'Bahaa', 'Wahba', 'Eldin', 'Selim',
]

REASONS = [
    'Persistent headache, photophobia',
    'Chest tightness on exertion',
    'Lower back pain, recurring',
    'Annual physical examination',
    'Post-op follow-up — knee arthroscopy',
    'Skin rash on forearm, itchy',
    'Routine pediatric checkup',
    'Shortness of breath at night',
    'Migraine, refractory to NSAIDs',
    'Hypertension medication review',
    'Type 2 diabetes follow-up',
    'Suspected stress fracture',
    'Vaccine — seasonal flu',
    'Dizziness and lightheadedness',
    'Upper respiratory infection',
    'Joint pain — bilateral knees',
    'Mole evaluation (dermoscopy)',
    'Ankle sprain, grade II',
    'Lab review — lipid panel',
    'Pre-operative consultation',
    'Anxiety and sleep disturbance',
    'Persistent cough, 3 weeks',
    'Eczema flare-up',
    'Heart palpitations during exercise',
    'Childhood asthma management',
]

DIAGNOSES = [
    'Tension-type headache', 'Migraine without aura', 'Stable angina',
    'Lumbar strain', 'Allergic contact dermatitis', 'Type 2 diabetes mellitus',
    'Essential hypertension', 'Generalized anxiety disorder',
    'Upper respiratory tract infection', 'Osteoarthritis (knee)',
    'Atopic dermatitis', 'Asthma — mild persistent', 'GERD',
    'Vitamin D deficiency', 'Iron-deficiency anemia',
]


def _random_phone():
    return f"01{random.choice('0125')}-{random.randint(1000000, 9999999)}"


def _random_email(name):
    base = name.lower().replace(' ', '.').replace("'", '')
    return f"{base}.{random.randint(1,99)}@smarthospital.eg"


def _random_dob(min_age=2, max_age=82):
    today = timezone.localdate()
    age_days = random.randint(min_age * 365, max_age * 365)
    return today - timedelta(days=age_days)


def _random_appt_dt(months_back=6, months_fwd=2):
    now = timezone.localtime()
    span_back = months_back * 30
    span_fwd = months_fwd * 30
    delta_days = random.randint(-span_back, span_fwd)
    base = now + timedelta(days=delta_days)
    return base.replace(
        hour=random.randint(8, 17),
        minute=random.choice([0, 15, 30, 45]),
        second=0, microsecond=0,
    )


class Command(BaseCommand):
    help = 'Seed the database with realistic demo data'

    def add_arguments(self, parser):
        parser.add_argument('--patients',     type=int, default=60)
        parser.add_argument('--doctors',      type=int, default=20)
        parser.add_argument('--appointments', type=int, default=300)
        parser.add_argument('--reset', action='store_true',
                            help='Wipe existing patients/doctors/appointments first')

    @transaction.atomic
    def handle(self, *args, **opts):
        random.seed(42)
        if opts['reset']:
            self.stdout.write(self.style.WARNING('Wiping existing data...'))
            AIPrediction.objects.all().delete()
            Appointment.objects.all().delete()
            Patient.objects.all().delete()
            Doctor.objects.all().delete()
            Department.objects.all().delete()
            AuditEvent.objects.all().delete()

        # ── Departments ────────────────────────────────────────────
        dept_objs = []
        for name, desc in DEPARTMENTS:
            d, _ = Department.objects.get_or_create(name=name, defaults={'description': desc})
            dept_objs.append(d)
        self.stdout.write(self.style.SUCCESS(f'Departments: {len(dept_objs)}'))

        # ── Doctors ────────────────────────────────────────────────
        existing_emails = set(Doctor.objects.values_list('email', flat=True))
        new_doctors = []
        target_doctors = max(opts['doctors'], Doctor.objects.count())
        while Doctor.objects.count() < target_doctors:
            dept = random.choice(dept_objs)
            full = f"{random.choice(DOCTOR_FIRST)} {random.choice(DOCTOR_LAST)} {random.choice(DOCTOR_LAST)}"
            email = _random_email(full)
            if email in existing_emails:
                continue
            existing_emails.add(email)
            doc = Doctor.objects.create(
                department=dept, name=full,
                specialty=random.choice(SPECIALTY_BY_DEPT[dept.name]),
                email=email,
            )
            new_doctors.append(doc)
        all_doctors = list(Doctor.objects.all())
        self.stdout.write(self.style.SUCCESS(f'Doctors: total {len(all_doctors)} (+{len(new_doctors)} new)'))

        # ── Doctor availability windows ───────────────────────────
        # Give every doctor a deterministic-ish weekly schedule so the
        # patient booking flow has slots to show on day one.
        avail_added = 0
        for doc in all_doctors:
            if doc.availabilities.exists():
                continue
            rng = random.Random(doc.pk)
            # Pick 3–5 weekdays (Sun=6, Mon=0 .. Fri=4 — skip Saturday=5 sometimes)
            weekdays = rng.sample(range(0, 7), k=rng.randint(3, 5))
            for wd in weekdays:
                # Morning + maybe afternoon
                DoctorAvailability.objects.create(
                    doctor=doc, weekday=wd,
                    start_time=time(rng.choice([8, 9, 10])),
                    end_time=time(12),
                    slot_minutes=rng.choice([20, 30]),
                )
                avail_added += 1
                if rng.random() < 0.65:
                    DoctorAvailability.objects.create(
                        doctor=doc, weekday=wd,
                        start_time=time(rng.choice([13, 14])),
                        end_time=time(rng.choice([16, 17, 18])),
                        slot_minutes=rng.choice([20, 30]),
                    )
                    avail_added += 1
        self.stdout.write(self.style.SUCCESS(f'Availability windows: +{avail_added} new'))

        # ── Patients ───────────────────────────────────────────────
        new_patients = []
        target_patients = max(opts['patients'], Patient.objects.count())
        while Patient.objects.count() < target_patients:
            if random.random() < 0.5:
                first = random.choice(PATIENT_FIRST_M)
            else:
                first = random.choice(PATIENT_FIRST_F)
            full = f"{first} {random.choice(PATIENT_LAST)} {random.choice(PATIENT_LAST)}"
            p = Patient.objects.create(name=full, date_of_birth=_random_dob(), phone=_random_phone())
            new_patients.append(p)
        all_patients = list(Patient.objects.all())
        self.stdout.write(self.style.SUCCESS(f'Patients: total {len(all_patients)} (+{len(new_patients)} new)'))

        # ── Appointments ──────────────────────────────────────────
        target_appts = opts['appointments']
        existing_appt_count = Appointment.objects.count()
        to_create = max(0, target_appts - existing_appt_count)
        new_appts = []
        now = timezone.localtime()
        for _ in range(to_create):
            dt = _random_appt_dt()
            if dt > now:
                status = 'Scheduled'
            else:
                status = random.choices(
                    ['Completed', 'Canceled', 'Scheduled'],
                    weights=[0.78, 0.15, 0.07])[0]
            appt = Appointment(
                doctor=random.choice(all_doctors),
                patient=random.choice(all_patients),
                date_time=dt,
                reason=random.choice(REASONS),
                status=status,
            )
            new_appts.append(appt)
        Appointment.objects.bulk_create(new_appts)
        self.stdout.write(self.style.SUCCESS(
            f'Appointments: total {Appointment.objects.count()} (+{len(new_appts)} new)'
        ))

        # ── AI Predictions on past appointments ───────────────────
        # Only attach predictions to appointments that don't have one yet
        past_appts = list(
            Appointment.objects.filter(date_time__lte=now)
            .exclude(predictions__status='SUCCESS')
        )
        random.shuffle(past_appts)
        # Cover ~70% of past appointments
        coverage_target = int(len(past_appts) * 0.7)
        new_preds = 0
        for appt in past_appts[:coverage_target]:
            if random.random() < 0.92:
                # Successful prediction
                conf = round(random.triangular(0.35, 0.95, 0.78), 2)
                AIPrediction.objects.create(
                    appointment=appt,
                    predicted_diagnosis=random.choice(DIAGNOSES),
                    confidence_score=conf,
                    model_version='lightning-ai/deepseek-v4-pro',
                    status='SUCCESS',
                    error_message='',
                    latency_ms=random.randint(420, 2400),
                    total_tokens=random.randint(160, 520),
                )
            else:
                AIPrediction.objects.create(
                    appointment=appt,
                    predicted_diagnosis='',
                    confidence_score=0.0,
                    model_version='lightning-ai/deepseek-v4-pro',
                    status='FAILED',
                    error_message='Rate limit exceeded',
                    latency_ms=random.randint(80, 200),
                    total_tokens=0,
                )
            new_preds += 1
        self.stdout.write(self.style.SUCCESS(f'Predictions: +{new_preds} new'))

        self.stdout.write(self.style.SUCCESS('\n✓ Demo data ready.'))
        self.stdout.write(
            f"  → patients: {Patient.objects.count()}, doctors: {Doctor.objects.count()}, "
            f"appointments: {Appointment.objects.count()}, predictions: {AIPrediction.objects.count()}"
        )
