# ADR 0001 — Patient identity model: OneToOne on existing Patient

**Status**: Accepted (2026-05-11)

## Context

SmartHospital began as a staff-only admin tool: a single Django `User` table where everybody was treated as a clinician, and `Patient` rows were data the staff entered about non-users. When we decided to add a patient-facing portal, we needed to give patients a login while keeping every existing `Patient` row, all `Appointment` foreign keys, and all 29 tests intact.

Three options were considered:

1. **OneToOneField from Patient → User**, with `null=True` so existing rows stay valid. Patients register via a form that creates the `User` and a linked `Patient`. (chosen)
2. **Replace `User` with a custom user model that has a `role` field**. Most flexible long-term but requires a full DB reset or a complex `AUTH_USER_MODEL` migration; would invalidate existing `auth_user` rows.
3. **Django Groups (Staff vs Patient)**. Easiest, but no first-class "this user IS patient #42" link — every page would need to query both `User` and `Patient` separately and stitch them.

## Decision

Add `Patient.user = OneToOneField('auth.User', on_delete=SET_NULL, null=True, blank=True, related_name='patient')` and `Patient.email = EmailField(blank=True)`. Role is derived from `User.is_staff`:

- `is_staff=True` → clinician
- `is_staff=False` AND has `user.patient` → patient

Decorators `staff_required` and `patient_required` (in `hospital/permissions.py`) enforce this throughout the codebase.

## Consequences

**Good**

- Existing 15-row Patient table keeps working untouched (`user` is nullable).
- `request.user.patient` is a clean accessor in every view and template.
- We did NOT have to reset the auth DB or migrate `auth_user`.
- All 29 pre-existing tests still pass.
- The chatbot's `_system_prompt` can branch on `session.user.is_staff` and `hasattr(session.user, 'patient')` for privacy scoping (Phase 4).

**Trade-offs**

- A future "doctor" login model (so doctors can also self-serve their schedule) would be slightly awkward: we'd either add another OneToOne (`Doctor.user`) or finally bite the bullet on a custom user model.
- `Patient.user` being nullable means we must check `hasattr(user, 'patient')` defensively in patient-only paths — captured in the `patient_required` decorator.

## When to revisit

Switch to a custom user model if:

- We add a third role (e.g. external lab staff, insurance reviewers) — the role-field approach starts paying off at three+ roles.
- We need fields on the user record itself (avatar URL, language preference) that don't fit neatly on Patient/Doctor.
