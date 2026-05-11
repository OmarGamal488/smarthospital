"""Role-based access decorators.

Three roles are derived from the standard Django User model:
- STAFF  : User.is_staff = True  (admins, receptionists). Sees everything.
- DOCTOR : User.is_staff = False AND has a linked Doctor  (User.doctor).
- PATIENT: User.is_staff = False AND has a linked Patient (User.patient).

Anonymous users always go to /login/.
"""

from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect


def _has_doctor(u) -> bool:
    return hasattr(u, 'doctor') and u.doctor is not None


def _has_patient(u) -> bool:
    return hasattr(u, 'patient') and u.patient is not None


def staff_required(view):
    """Allow only authenticated staff users; bounce others appropriately."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        u = request.user
        if not u.is_authenticated:
            return redirect('login')
        if not u.is_staff:
            messages.error(request, 'That area is for staff only.')
            if _has_doctor(u):
                return redirect('doctor_home')
            if _has_patient(u):
                return redirect('patient_home')
            return redirect('login')
        return view(request, *args, **kwargs)
    return wrapper


def patient_required(view):
    """Allow only authenticated patients (non-staff with a linked Patient)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        u = request.user
        if not u.is_authenticated:
            return redirect('patient_login')
        if u.is_staff:
            return redirect('dashboard')
        if _has_doctor(u):
            return redirect('doctor_home')
        if not _has_patient(u):
            messages.error(request, 'Your patient profile is incomplete. Please contact reception.')
            return redirect('patient_login')
        return view(request, *args, **kwargs)
    return wrapper


def doctor_required(view):
    """Allow only authenticated doctors (non-staff with a linked Doctor)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        u = request.user
        if not u.is_authenticated:
            return redirect('doctor_login')
        if u.is_staff:
            return redirect('dashboard')
        if _has_patient(u) and not _has_doctor(u):
            return redirect('patient_home')
        if not _has_doctor(u):
            messages.error(request, 'Your doctor profile is not linked. Contact an administrator.')
            return redirect('doctor_login')
        return view(request, *args, **kwargs)
    return wrapper
