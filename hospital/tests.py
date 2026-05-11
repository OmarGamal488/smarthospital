"""SmartHospital test suite — models, signals, views, bulk actions, chatbot."""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AIPrediction,
    Appointment,
    AuditEvent,
    ChatMessage,
    ChatSession,
    Department,
    Doctor,
    Patient,
)


def _make_fixtures():
    dept = Department.objects.create(name='Cardiology', description='Heart')
    doc  = Doctor.objects.create(department=dept, name='Idris Khan',
                                 specialty='Cardiology', email='ik@x.com')
    pat  = Patient.objects.create(name='Elena Marsh',
                                  date_of_birth=date(1985, 5, 14), phone='555-0100')
    appt = Appointment.objects.create(
        doctor=doc, patient=pat,
        date_time=timezone.now() + timedelta(days=1),
        reason='Persistent migraines', status='Scheduled',
    )
    return dept, doc, pat, appt


# ── Model tests ─────────────────────────────────────────────────────────────


class ModelTests(TestCase):
    def setUp(self):
        self.dept, self.doc, self.pat, self.appt = _make_fixtures()

    def test_str_methods(self):
        self.assertEqual(str(self.dept), 'Cardiology')
        self.assertIn('Idris Khan', str(self.doc))
        self.assertEqual(str(self.pat), 'Elena Marsh')
        self.assertIn('Elena', str(self.appt))

    def test_latest_prediction_returns_only_success(self):
        AIPrediction.objects.create(
            appointment=self.appt, predicted_diagnosis='X',
            confidence_score=0.4, model_version='m1', status='FAILED',
        )
        p2 = AIPrediction.objects.create(
            appointment=self.appt, predicted_diagnosis='Migraine',
            confidence_score=0.88, model_version='m1', status='SUCCESS',
        )
        self.assertEqual(self.appt.latest_prediction.pk, p2.pk)

    def test_latest_prediction_empty_when_no_success(self):
        AIPrediction.objects.create(
            appointment=self.appt, predicted_diagnosis='X',
            confidence_score=0.0, model_version='m1', status='FAILED',
        )
        self.assertIsNone(self.appt.latest_prediction)


# ── Audit log signals ───────────────────────────────────────────────────────


class AuditSignalTests(TestCase):
    def test_create_logs_audit_event(self):
        Patient.objects.create(name='X', date_of_birth=date(2000, 1, 1), phone='1')
        ev = AuditEvent.objects.filter(kind='patient', action='create').first()
        self.assertIsNotNone(ev)
        self.assertIn('Patient X', ev.summary)

    def test_update_logs_update_event(self):
        p = Patient.objects.create(name='X', date_of_birth=date(2000, 1, 1), phone='1')
        AuditEvent.objects.all().delete()
        p.phone = '2'
        p.save()
        ev = AuditEvent.objects.filter(kind='patient', action='update').first()
        self.assertIsNotNone(ev)

    def test_delete_logs_delete_event(self):
        p = Patient.objects.create(name='X', date_of_birth=date(2000, 1, 1), phone='1')
        AuditEvent.objects.all().delete()
        p.delete()
        ev = AuditEvent.objects.filter(kind='patient', action='delete').first()
        self.assertIsNotNone(ev)

    def test_prediction_logs_analyze_action(self):
        _, _, _, appt = _make_fixtures()
        AuditEvent.objects.all().delete()
        AIPrediction.objects.create(
            appointment=appt, predicted_diagnosis='X',
            confidence_score=0.6, model_version='m', status='SUCCESS',
        )
        ev = AuditEvent.objects.filter(kind='prediction', action='analyze').first()
        self.assertIsNotNone(ev)


# ── View tests ──────────────────────────────────────────────────────────────


class ViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('tester', 'tester@x.com', 'pass1234!', is_staff=True)
        cls.dept, cls.doc, cls.pat, cls.appt = _make_fixtures()

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_dashboard_default_range_is_30d(self):
        r = self.client.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Last 30 days')

    def test_dashboard_range_filter(self):
        for key, label in [('today', 'Today'), ('7d', 'Last 7 days'),
                           ('all', 'All time')]:
            r = self.client.get(reverse('dashboard') + f'?range={key}')
            self.assertContains(r, label)

    def test_dashboard_invalid_range_falls_back(self):
        r = self.client.get(reverse('dashboard') + '?range=garbage')
        self.assertContains(r, 'Last 30 days')

    def test_patient_detail_renders(self):
        r = self.client.get(reverse('patient_detail', args=[self.pat.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.pat.name)

    def test_patient_detail_404(self):
        r = self.client.get(reverse('patient_detail', args=[999999]))
        self.assertEqual(r.status_code, 404)

    def test_doctor_detail_renders(self):
        r = self.client.get(reverse('doctor_detail', args=[self.doc.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.doc.name)

    def test_calendar_year_boundaries(self):
        r = self.client.get(reverse('appointments_calendar') + '?year=2026&month=1')
        self.assertContains(r, 'year=2025&month=12')
        r = self.client.get(reverse('appointments_calendar') + '?year=2026&month=12')
        self.assertContains(r, 'year=2027&month=1')

    def test_appointment_create_prefills_patient_and_doctor(self):
        r = self.client.get(reverse('appointment_create')
                            + f'?patient={self.pat.pk}&doctor={self.doc.pk}')
        body = r.content.decode()
        self.assertIn(f'value="{self.pat.pk}" selected', body)
        self.assertIn(f'value="{self.doc.pk}" selected', body)

    def test_login_required(self):
        c = Client()
        r = c.get(reverse('dashboard'))
        # Redirects to login
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login/', r.headers['Location'])


# ── Bulk action tests ───────────────────────────────────────────────────────


class BulkActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('bulker', 'b@x.com', 'pass1234!', is_staff=True)
        cls.dept = Department.objects.create(name='X')
        cls.doc  = Doctor.objects.create(department=cls.dept, name='D',
                                          specialty='S', email='d@x.com')
        cls.pat  = Patient.objects.create(name='P', date_of_birth=date(2000, 1, 1), phone='1')

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.appts = [Appointment.objects.create(
            doctor=self.doc, patient=self.pat,
            date_time=timezone.now() + timedelta(days=i),
            reason=f'reason {i}', status='Scheduled',
        ) for i in range(3)]

    def test_bulk_complete(self):
        ids = ','.join(str(a.pk) for a in self.appts)
        r = self.client.post(reverse('appointments_bulk_action'),
                             {'action': 'complete', 'ids': ids}, follow=True)
        self.assertEqual(r.status_code, 200)
        for a in self.appts:
            a.refresh_from_db()
            self.assertEqual(a.status, 'Completed')

    def test_bulk_cancel(self):
        ids = str(self.appts[0].pk)
        self.client.post(reverse('appointments_bulk_action'),
                         {'action': 'cancel', 'ids': ids})
        self.appts[0].refresh_from_db()
        self.assertEqual(self.appts[0].status, 'Canceled')

    def test_bulk_unknown_action_safe(self):
        r = self.client.post(reverse('appointments_bulk_action'),
                             {'action': 'destroy', 'ids': str(self.appts[0].pk)},
                             follow=True)
        self.assertEqual(r.status_code, 200)
        self.appts[0].refresh_from_db()
        self.assertEqual(self.appts[0].status, 'Scheduled')

    def test_bulk_no_ids(self):
        r = self.client.post(reverse('appointments_bulk_action'),
                             {'action': 'complete', 'ids': ''},
                             follow=True)
        self.assertEqual(r.status_code, 200)


# ── Chatbot tests (mocked LLM) ──────────────────────────────────────────────


class ChatbotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('chatter', 'c@x.com', 'pass1234!', is_staff=True)
        cls.other = User.objects.create_user('other', 'o@x.com', 'pass1234!', is_staff=True)
        cls.dept, cls.doc, cls.pat, cls.appt = _make_fixtures()

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_open_creates_session(self):
        r = self.client.get(reverse('chat_open') + '?kind=general')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['kind'], 'general')

    def test_open_idempotent(self):
        r1 = self.client.get(reverse('chat_open') + '?kind=general')
        r2 = self.client.get(reverse('chat_open') + '?kind=general')
        self.assertEqual(r1.json()['session_id'], r2.json()['session_id'])

    def test_open_invalid_kind_falls_back(self):
        r = self.client.get(reverse('chat_open') + '?kind=garbage')
        self.assertEqual(r.json()['kind'], 'general')

    def test_send_persists_messages(self):
        sid = self.client.get(reverse('chat_open') + '?kind=general').json()['session_id']

        class FakeAI:
            content = 'Hi there.'
            tool_calls = []
            usage_metadata = {'total_tokens': 12}

        with patch('hospital.chat_service.ChatOpenAI') as MockLLM:
            inst = MagicMock()
            inst.bind_tools.return_value = inst
            inst.invoke.return_value = FakeAI()
            MockLLM.return_value = inst
            r = self.client.post(
                reverse('chat_send', args=[sid]),
                data=json.dumps({'message': 'Hello'}),
                content_type='application/json',
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['reply'], 'Hi there.')
        sess = ChatSession.objects.get(pk=sid)
        self.assertEqual(sess.messages.filter(role='user').count(), 1)
        self.assertEqual(sess.messages.filter(role='assistant').count(), 1)

    def test_send_empty_returns_400(self):
        sid = self.client.get(reverse('chat_open') + '?kind=general').json()['session_id']
        r = self.client.post(reverse('chat_send', args=[sid]),
                             data=json.dumps({'message': ''}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_send_other_users_session_404(self):
        sid = self.client.get(reverse('chat_open') + '?kind=general').json()['session_id']
        c2 = Client(); c2.force_login(self.other)
        r = c2.post(reverse('chat_send', args=[sid]),
                    data=json.dumps({'message': 'hi'}),
                    content_type='application/json')
        self.assertEqual(r.status_code, 404)

    def test_reset_wipes_messages(self):
        sid = self.client.get(reverse('chat_open') + '?kind=general').json()['session_id']
        ChatMessage.objects.create(session_id=sid, role='user', content='x')
        r = self.client.post(reverse('chat_reset', args=[sid]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ChatMessage.objects.filter(session_id=sid).count(), 0)


# ── Notifications ───────────────────────────────────────────────────────────


class NotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('notif', 'n@x.com', 'pass1234!', is_staff=True)
        cls.dept, cls.doc, cls.pat, cls.appt = _make_fixtures()
        # Add a low-confidence prediction so the notification list is non-empty
        AIPrediction.objects.create(
            appointment=cls.appt, predicted_diagnosis='Concerning',
            confidence_score=0.32, model_version='m', status='SUCCESS',
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_count_endpoint(self):
        r = self.client.get(reverse('notifications_count'))
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()['count'], 1)

    def test_panel_renders(self):
        r = self.client.get(reverse('notifications'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Concerning')


# ── Patient registration ────────────────────────────────────────────────────


class PatientRegistrationTests(TestCase):
    def test_register_creates_user_and_patient(self):
        c = Client()
        r = c.post(reverse('patient_register'), {
            'username':       'newpat',
            'full_name':      'New Patient',
            'date_of_birth':  '1990-04-01',
            'phone':          '01000000000',
            'email':          'np@example.com',
            'password1':      'StrongPass123!',
            'password2':      'StrongPass123!',
        })
        self.assertEqual(r.status_code, 302)
        u = User.objects.get(username='newpat')
        self.assertFalse(u.is_staff)
        self.assertTrue(hasattr(u, 'patient') and u.patient is not None)
        self.assertEqual(u.patient.email, 'np@example.com')
        self.assertEqual(u.patient.name, 'New Patient')

    def test_register_duplicate_email_rejected(self):
        Patient.objects.create(
            name='Existing', date_of_birth=date(1980, 1, 1),
            phone='1', email='dupe@example.com',
        )
        c = Client()
        r = c.post(reverse('patient_register'), {
            'username':       'dupeuser',
            'full_name':      'Dupe',
            'date_of_birth':  '1990-01-01',
            'phone':          '02',
            'email':          'dupe@example.com',
            'password1':      'StrongPass123!',
            'password2':      'StrongPass123!',
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'already registered')


# ── Booking flow ────────────────────────────────────────────────────────────


class BookingFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dept = Department.objects.create(name='Cardio')
        cls.doc  = Doctor.objects.create(department=cls.dept, name='D',
                                         specialty='Cardiology', email='d@x.com')
        cls.user = User.objects.create_user('p1', 'p1@x.com', 'StrongPass123!')
        cls.user.is_staff = False
        cls.user.save()
        cls.patient = Patient.objects.create(
            user=cls.user, name='P One',
            date_of_birth=date(1985, 1, 1), phone='1', email='p1@x.com',
        )
        from .models import DoctorAvailability
        # Availability every weekday 09:00–11:00, 30-min slots
        for wd in range(7):
            DoctorAvailability.objects.create(
                doctor=cls.doc, weekday=wd,
                start_time=__import__('datetime').time(9, 0),
                end_time=__import__('datetime').time(11, 0),
                slot_minutes=30,
            )

    def test_available_slots_drops_taken(self):
        from .booking import available_slots
        # Pick a target date 5 days in the future to avoid the "skip past slots" rule.
        target = (timezone.localdate() + timedelta(days=5))
        # Book a 09:30 appointment
        Appointment.objects.create(
            doctor=self.doc, patient=self.patient,
            date_time=timezone.make_aware(
                __import__('datetime').datetime.combine(
                    target, __import__('datetime').time(9, 30)
                )
            ),
            reason='x', status='Scheduled',
        )
        slots = available_slots(self.doc, target)
        # Slots are 9:00, 9:30, 10:00, 10:30 — but 9:30 is taken
        self.assertEqual(len(slots), 3)
        hours = [s.strftime('%H:%M') for s in slots]
        self.assertIn('09:00', hours)
        self.assertNotIn('09:30', hours)
        self.assertIn('10:00', hours)
        self.assertIn('10:30', hours)

    def test_patient_can_view_book_page(self):
        c = Client(); c.force_login(self.user)
        r = c.get(reverse('patient_book'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.dept.name)


# ── Permission boundaries ───────────────────────────────────────────────────


class PermissionBoundaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user('staff1', 's@x.com', 'pw1234!', is_staff=True)
        cls.pat_user = User.objects.create_user('pat1', 'pat1@x.com', 'pw1234!')
        cls.pat_user.is_staff = False
        cls.pat_user.save()
        cls.dept = Department.objects.create(name='Gen')
        cls.doc  = Doctor.objects.create(department=cls.dept, name='D',
                                         specialty='S', email='d@x.com')
        cls.patient = Patient.objects.create(
            user=cls.pat_user, name='P', date_of_birth=date(2000,1,1), phone='1',
        )
        cls.other_pat = Patient.objects.create(
            name='Other', date_of_birth=date(1995,1,1), phone='2',
        )

    def test_patient_blocked_from_dashboard(self):
        c = Client(); c.force_login(self.pat_user)
        r = c.get(reverse('dashboard'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/patient/', r.headers['Location'])

    def test_staff_redirected_from_patient_home(self):
        c = Client(); c.force_login(self.staff)
        r = c.get(reverse('patient_home'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('/dashboard/', r.headers['Location'])

    def test_patient_cannot_view_other_patients_appointment(self):
        appt = Appointment.objects.create(
            doctor=self.doc, patient=self.other_pat,
            date_time=timezone.now() + timedelta(days=1),
            reason='x', status='Scheduled',
        )
        c = Client(); c.force_login(self.pat_user)
        r = c.get(reverse('patient_appointment_detail', args=[appt.pk]))
        self.assertEqual(r.status_code, 404)


# ── LLM robustness ──────────────────────────────────────────────────────────


class LLMRobustnessTests(TestCase):
    def test_predict_diagnosis_no_key(self):
        from django.test import override_settings
        from .ai_service import predict_diagnosis
        dept = Department.objects.create(name='X')
        doc  = Doctor.objects.create(department=dept, name='D', specialty='S', email='r@x.com')
        pat  = Patient.objects.create(name='P', date_of_birth=date(2000,1,1), phone='1')
        appt = Appointment.objects.create(
            doctor=doc, patient=pat,
            date_time=timezone.now() + timedelta(days=1),
            reason='x', status='Scheduled',
        )
        with override_settings(LIGHTNING_API_KEY=None):
            result = predict_diagnosis(appt)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('not configured', result['error_message'])


# ── Email confirmation ──────────────────────────────────────────────────────


class EmailConfirmationTests(TestCase):
    def test_send_appointment_confirmed(self):
        from django.core import mail
        from .notifications import send_appointment_confirmed
        dept = Department.objects.create(name='X')
        doc  = Doctor.objects.create(department=dept, name='D', specialty='S', email='d@x.com')
        pat  = Patient.objects.create(
            name='Mailable', date_of_birth=date(2000, 1, 1),
            phone='1', email='mailable@x.com',
        )
        appt = Appointment.objects.create(
            doctor=doc, patient=pat,
            date_time=timezone.now() + timedelta(days=1),
            reason='Annual', status='Scheduled',
        )
        mail.outbox.clear()
        ok = send_appointment_confirmed(appt)
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn('mailable@x.com', msg.to)
        self.assertIn('Dr. D', msg.subject)
        self.assertIn('Annual', msg.body)

    def test_no_email_when_no_address(self):
        from .notifications import send_appointment_confirmed
        dept = Department.objects.create(name='X')
        doc  = Doctor.objects.create(department=dept, name='D', specialty='S', email='d@x.com')
        pat  = Patient.objects.create(name='Q', date_of_birth=date(2000, 1, 1), phone='1')
        appt = Appointment.objects.create(
            doctor=doc, patient=pat,
            date_time=timezone.now() + timedelta(days=1),
            reason='r', status='Scheduled',
        )
        self.assertFalse(send_appointment_confirmed(appt))


# ── Healthcheck ─────────────────────────────────────────────────────────────


class HealthcheckTests(TestCase):
    def test_healthz(self):
        c = Client()
        r = c.get(reverse('healthz'))
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['db'])
