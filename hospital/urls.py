from django.urls import path
from django.views.generic import RedirectView
from django.contrib.auth import views as auth_views
from . import views, views_doctor, views_patient

urlpatterns = [
    path('', views.home_router, name='home'),
    path('healthz/', views.healthz, name='healthz'),
    path('welcome/', views.landing, name='welcome'),
    path('dashboard/', views.dashboard, name='dashboard'),

    # Departments
    path('departments/',                 views.departments,         name='departments'),
    path('departments/add/',             views.department_create,   name='department_create'),
    path('departments/<int:pk>/',        views.department_detail,   name='department_detail'),
    path('departments/<int:pk>/edit/',   views.department_edit,     name='department_edit'),
    path('departments/<int:pk>/delete/', views.department_delete,   name='department_delete'),

    # Doctors
    path('doctors/',            views.doctors,       name='doctors'),
    path('doctors/add/',        views.doctor_create, name='doctor_create'),
    path('doctors/<int:pk>/',        views.doctor_detail, name='doctor_detail'),
    path('doctors/<int:pk>/edit/',   views.doctor_edit,   name='doctor_edit'),
    path('doctors/<int:pk>/delete/', views.doctor_delete, name='doctor_delete'),

    # Patients
    path('patients/',            views.patients,       name='patients'),
    path('patients/add/',        views.patient_create, name='patient_create'),
    path('patients/<int:pk>/',        views.patient_detail, name='patient_detail'),
    path('patients/<int:pk>/edit/',   views.patient_edit,   name='patient_edit'),
    path('patients/<int:pk>/delete/', views.patient_delete, name='patient_delete'),

    # Appointments
    path('appointments/',                   views.appointments,         name='appointments'),
    path('appointments/calendar/',          views.appointments_calendar, name='appointments_calendar'),
    path('appointments/add/',               views.appointment_create,   name='appointment_create'),
    path('appointments/<int:pk>/edit/',     views.appointment_edit,     name='appointment_edit'),
    path('appointments/<int:pk>/delete/',   views.appointment_delete,   name='appointment_delete'),
    path('appointments/<int:pk>/predict/',      views.appointment_predict,      name='appointment_predict'),
    path('appointments/<int:pk>/predictions/', views.appointment_predictions,  name='appointment_predictions'),
    path('appointments/bulk-predict/',         views.bulk_predict,             name='bulk_predict'),
    path('appointments/bulk-analyze/',         views.bulk_predict,             name='appointments_bulk_analyze'),
    path('appointments/bulk-action/',          views.appointments_bulk_action, name='appointments_bulk_action'),

    # CSV exports
    path('appointments/export/', views.export_appointments_csv, name='export_appointments'),
    path('appointments/export-csv/', views.export_appointments_csv, name='appointments_export'),
    path('patients/export/',     views.export_patients_csv,     name='export_patients'),
    path('patients/export-csv/', views.export_patients_csv, name='patients_export'),

    # AI insight (dashboard card)
    path('dashboard/ai-insight/', views.dashboard_ai_insight, name='dashboard_ai_insight'),

    # Notifications
    path('notifications/',       views.notifications,       name='notifications'),
    path('notifications/count/', views.notifications_count, name='notifications_count'),

    # Chatbot
    path('chat/open/',                     views.chat_open,  name='chat_open'),
    path('chat/<int:session_id>/send/',    views.chat_send,  name='chat_send'),
    path('chat/<int:session_id>/reset/',   views.chat_reset, name='chat_reset'),

    # ── Public services (no login) ───────────────────────────
    path('services/',           views_patient.services,            name='services'),
    path('services/<int:pk>/',  views_patient.services_department, name='services_department'),

    # ── Patient portal ───────────────────────────────────────
    path('patient/register/', views_patient.patient_register, name='patient_register'),
    path('patient/login/',    auth_views.LoginView.as_view(
        template_name='hospital/patient/login.html',
        redirect_authenticated_user=True,
    ), name='patient_login'),
    path('patient/',                          views_patient.patient_home,                name='patient_home'),
    path('patient/profile/',                  views_patient.patient_profile,             name='patient_profile'),
    path('patient/profile/telegram-code/',    views_patient.patient_telegram_code,       name='patient_telegram_code'),
    path('patient/book/',                     views_patient.patient_book,                name='patient_book'),
    path('patient/book/dept/<int:dept_pk>/',  views_patient.patient_book_doctor,         name='patient_book_doctor'),
    path('patient/book/doctor/<int:doctor_pk>/slots/', views_patient.patient_book_slots, name='patient_book_slots'),
    path('patient/book/doctor/<int:doctor_pk>/confirm/', views_patient.patient_book_confirm, name='patient_book_confirm'),
    path('patient/ai/symptoms/',              views_patient.patient_ai_symptoms,         name='patient_ai_symptoms'),
    path('patient/appointments/',             views_patient.patient_appointments,        name='patient_appointments'),
    path('patient/appointments/<int:pk>/',    views_patient.patient_appointment_detail,  name='patient_appointment_detail'),
    path('patient/appointments/<int:pk>/cancel/',     views_patient.patient_appointment_cancel,     name='patient_appointment_cancel'),
    path('patient/appointments/<int:pk>/reschedule/', views_patient.patient_appointment_reschedule, name='patient_appointment_reschedule'),
    path('patient/appointments/<int:pk>/rate/',       views_patient.patient_appointment_rate,       name='patient_appointment_rate'),

    # ── Doctor portal ────────────────────────────────────────
    path('doctor/login/',    auth_views.LoginView.as_view(
        template_name='hospital/doctor/login.html',
        redirect_authenticated_user=True,
    ), name='doctor_login'),
    path('doctor/',                              views_doctor.doctor_home,                  name='doctor_home'),
    path('doctor/appointments/',                 views_doctor.doctor_appointments,          name='doctor_appointments'),
    path('doctor/appointments/<int:pk>/',        views_doctor.doctor_appointment_detail,    name='doctor_appointment_detail'),
    path('doctor/appointments/<int:pk>/status/', views_doctor.doctor_appointment_set_status, name='doctor_appointment_set_status'),
    path('doctor/appointments/<int:pk>/reschedule/', views_doctor.doctor_appointment_reschedule, name='doctor_appointment_reschedule'),
    path('doctor/availability/',                 views_doctor.doctor_availability,          name='doctor_availability'),
    path('doctor/availability/<int:pk>/delete/', views_doctor.doctor_availability_delete,   name='doctor_availability_delete'),
    path('doctor/timeoff/<int:pk>/delete/',      views_doctor.doctor_timeoff_delete,        name='doctor_timeoff_delete'),
    path('doctor/profile/',                      views_doctor.doctor_profile,               name='doctor_profile'),

    # ── Auth (staff) ─────────────────────────────────────────
    path('register/', views.register, name='register'),
    path('login/',    auth_views.LoginView.as_view(template_name='hospital/login.html', redirect_authenticated_user=True),  name='login'),
    path('logout/',   auth_views.LogoutView.as_view(), name='logout'),
]
