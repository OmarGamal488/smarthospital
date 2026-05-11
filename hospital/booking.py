"""Slot computation helpers for the patient-facing booking flow.

Rules:
- A doctor exposes weekly availability windows via DoctorAvailability rows.
- A "slot" is a fixed-length interval (slot_minutes) starting at the window's
  start_time and stepping forward until end_time.
- A slot is "taken" if any non-canceled Appointment for that doctor starts
  inside the slot's interval [start, start + slot_minutes).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.utils import timezone

from .models import Appointment, Doctor, DoctorTimeOff


def _combine_local(d: date, t: time) -> datetime:
    """Combine a date + time into a timezone-aware local datetime."""
    naive = datetime.combine(d, t)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def available_slots(doctor: Doctor, target_date: date) -> list[datetime]:
    """Open slot starts for `doctor` on `target_date`.

    Returns an empty list if the doctor has no availability that weekday.
    Slots in the past (relative to now) are excluded so today only shows
    upcoming slots.
    """
    # Doctor declared this whole day off → no slots.
    if DoctorTimeOff.objects.filter(doctor=doctor, date=target_date).exists():
        return []

    weekday = target_date.weekday()
    windows = doctor.availabilities.filter(weekday=weekday).order_by('start_time')
    if not windows.exists():
        return []

    now = timezone.localtime()
    candidates: list[datetime] = []
    for win in windows:
        cursor = _combine_local(target_date, win.start_time)
        end    = _combine_local(target_date, win.end_time)
        step   = timedelta(minutes=win.slot_minutes)
        while cursor + step <= end:
            if cursor > now:
                candidates.append(cursor)
            cursor += step

    if not candidates:
        return []

    # Exclude slots that overlap an existing non-canceled appointment.
    # A slot at start S "overlaps" if any appt has S <= appt.date_time < S + step.
    day_start = _combine_local(target_date, time.min)
    day_end   = _combine_local(target_date, time.max)
    booked = list(
        Appointment.objects.filter(
            doctor=doctor,
            date_time__gte=day_start,
            date_time__lte=day_end,
        ).exclude(status='Canceled').values_list('date_time', flat=True)
    )

    if not booked:
        return candidates

    # Use a permissive overlap check: drop any candidate whose interval contains
    # a booked datetime. We use the slot length from each window separately.
    open_slots: list[datetime] = []
    for win in windows:
        step = timedelta(minutes=win.slot_minutes)
        win_start = _combine_local(target_date, win.start_time)
        win_end   = _combine_local(target_date, win.end_time)
        for slot in candidates:
            if not (win_start <= slot < win_end):
                continue
            taken = any(slot <= b < slot + step for b in booked)
            if not taken:
                open_slots.append(slot)
    # Deduplicate (overlapping windows could double-count)
    seen: set = set()
    result: list[datetime] = []
    for s in sorted(open_slots):
        if s in seen:
            continue
        seen.add(s)
        result.append(s)
    return result


def next_available_days(doctor: Doctor, n: int = 7) -> list[date]:
    """First `n` calendar dates from today onward where this doctor has an open slot."""
    today = timezone.localdate()
    found: list[date] = []
    cursor = today
    for _ in range(n * 7):  # at most 7×n days lookahead
        if available_slots(doctor, cursor):
            found.append(cursor)
            if len(found) >= n:
                break
        cursor += timedelta(days=1)
    return found
