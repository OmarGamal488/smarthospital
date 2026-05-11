"""SmartHospital chatbot service.

Implements a tool-using LangChain agent that can answer questions about
patients, doctors, appointments, and AI predictions. Conversation history is
persisted in ChatMessage; each response runs a multi-turn tool-execution loop
until the model produces a final assistant message.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from .models import (
    AIPrediction,
    Appointment,
    ChatMessage,
    ChatSession,
    Department,
    Doctor,
    Patient,
)

MODEL    = "lightning-ai/deepseek-v4-pro"
BASE_URL = "https://lightning.ai/api/v1/"
MAX_TOOL_ITERS = 6
HISTORY_LIMIT  = 24

# ── Tools ───────────────────────────────────────────────────────────────────


@tool
def search_patients(query: str) -> str:
    """Search patients by name or phone number. Returns up to 10 matches with their IDs.

    Use this when the user mentions a patient by name and you need to find their record.
    For listing ALL patients (no name given), call list_all_patients instead.
    """
    query = (query or "").strip()
    if not query:
        return "Empty query — use list_all_patients to enumerate."
    qs = Patient.objects.filter(Q(name__icontains=query) | Q(phone__icontains=query))[:10]
    if not qs:
        return f"No patients matched '{query}'."
    return "\n".join(
        f"#{p.pk} · {p.name} · DOB {p.date_of_birth} · {p.phone}" for p in qs
    )


@tool
def list_all_patients(page: int = 1, page_size: int = 25) -> str:
    """List ALL patients (paginated). Use when the user asks to see all patients,
    enumerate the roster, or "show me everyone." page is 1-based, page_size 25 max."""
    page      = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 25), 25))
    total = Patient.objects.count()
    if total == 0:
        return "No patients in the system yet."
    start = (page - 1) * page_size
    qs = Patient.objects.order_by('name')[start:start + page_size]
    pages = (total + page_size - 1) // page_size
    lines = [f"Total patients: {total} · page {page}/{pages}"]
    for p in qs:
        lines.append(f"  #{p.pk} · {p.name} · {p.phone}")
    if page < pages:
        lines.append(f"\n(Use list_all_patients(page={page + 1}) for more.)")
    return "\n".join(lines)


@tool
def get_patient_summary(patient_id: int) -> str:
    """Get a full summary of one patient: demographics, total appointments, and recent visits.

    Use this to answer questions about a specific known patient's history.
    """
    try:
        p = Patient.objects.prefetch_related('appointments').get(pk=patient_id)
    except Patient.DoesNotExist:
        return f"Patient #{patient_id} not found."

    today = date.today()
    age = "unknown"
    if p.date_of_birth:
        age = today.year - p.date_of_birth.year - (
            (today.month, today.day) < (p.date_of_birth.month, p.date_of_birth.day)
        )

    appts = p.appointments.select_related('doctor').order_by('-date_time')[:5]
    status_counts = (p.appointments.values('status')
                                    .annotate(n=Count('id')))
    sc = {s['status']: s['n'] for s in status_counts}

    lines = [
        f"Patient #{p.pk}: {p.name}, age {age}",
        f"DOB: {p.date_of_birth} | Phone: {p.phone}",
        f"Totals — appointments {p.appointments.count()}; "
        f"completed {sc.get('Completed', 0)}, scheduled {sc.get('Scheduled', 0)}, "
        f"canceled {sc.get('Canceled', 0)}",
        "Recent visits:" if appts else "No recent visits.",
    ]
    for a in appts:
        lines.append(
            f"  - {a.date_time:%Y-%m-%d %H:%M} · Dr. {a.doctor.name} · "
            f"{a.status} · {a.reason[:80]}"
        )
    return "\n".join(lines)


@tool
def list_appointments(
    patient_id: int | None = None,
    doctor_id: int | None = None,
    days: int = 30,
    status: str = "",
) -> str:
    """List appointments. Optional filters: patient_id, doctor_id, last N days (default 30), status (Scheduled / Completed / Canceled).

    Use this for questions like "what's on Dr. X's calendar this week" or "show me Joe's upcoming visits".
    """
    qs = Appointment.objects.select_related('patient', 'doctor')
    if patient_id:
        qs = qs.filter(patient_id=patient_id)
    if doctor_id:
        qs = qs.filter(doctor_id=doctor_id)
    if status:
        qs = qs.filter(status__iexact=status)
    if days and days > 0:
        cutoff = timezone.now() - timedelta(days=days)
        qs = qs.filter(date_time__gte=cutoff)
    qs = qs.order_by('-date_time')[:10]

    if not qs:
        return "No matching appointments."
    return "\n".join(
        f"#{a.pk} · {a.date_time:%Y-%m-%d %H:%M} · {a.patient.name} ↔ Dr. {a.doctor.name} · {a.status} · {a.reason[:60]}"
        for a in qs
    )


@tool
def get_ai_predictions(appointment_id: int) -> str:
    """Get the AI prediction history for a specific appointment, including diagnosis text and confidence scores.

    Use this when the user asks 'what did the AI say about appointment N?' or wants to review confidence.
    """
    try:
        appt = Appointment.objects.select_related('patient', 'doctor').get(pk=appointment_id)
    except Appointment.DoesNotExist:
        return f"Appointment #{appointment_id} not found."
    preds = appt.predictions.order_by('-created_at')[:5]
    head = (
        f"Appointment #{appt.pk}: {appt.patient.name} with Dr. {appt.doctor.name} "
        f"on {appt.date_time:%Y-%m-%d}. Reason: {appt.reason[:100]}"
    )
    if not preds:
        return head + "\nNo AI predictions yet."
    lines = [head, "Predictions:"]
    for p in preds:
        if p.status == 'SUCCESS':
            lines.append(
                f"  - {p.created_at:%Y-%m-%d %H:%M} · {p.predicted_diagnosis[:100]} · "
                f"confidence {int(p.confidence_score * 100)}% · {p.model_version}"
            )
        else:
            lines.append(f"  - {p.created_at:%Y-%m-%d %H:%M} · FAILED: {p.error_message[:80]}")
    return "\n".join(lines)


@tool
def search_doctors(query: str) -> str:
    """Search doctors by name or specialty. Returns up to 10 matches with their departments.

    For listing ALL doctors (no name/specialty given), call list_all_doctors instead.
    """
    query = (query or "").strip()
    if not query:
        return "Empty query — use list_all_doctors to enumerate."
    qs = (Doctor.objects.select_related('department')
                        .filter(Q(name__icontains=query) | Q(specialty__icontains=query))[:10])
    if not qs:
        return f"No doctors matched '{query}'."
    return "\n".join(
        f"#{d.pk} · Dr. {d.name} · {d.specialty} · {d.department.name}" for d in qs
    )


@tool
def list_all_doctors() -> str:
    """List ALL doctors with specialty and department. Use when the user asks to see
    all doctors, the care team, or "who works here."""
    qs = Doctor.objects.select_related('department').order_by('department__name', 'name')
    total = qs.count()
    if total == 0:
        return "No doctors in the system yet."
    lines = [f"Total doctors: {total}"]
    current_dept = None
    for d in qs:
        if d.department.name != current_dept:
            current_dept = d.department.name
            lines.append(f"\n{current_dept}:")
        lines.append(f"  #{d.pk} · Dr. {d.name} · {d.specialty}")
    return "\n".join(lines)


@tool
def list_departments() -> str:
    """List all departments in the hospital with the count of doctors in each."""
    qs = Department.objects.annotate(n=Count('doctors')).order_by('name')
    if not qs:
        return "No departments configured."
    return "Departments:\n" + "\n".join(
        f"  · {d.name} ({d.n} doctor{'s' if d.n != 1 else ''})" for d in qs
    )


@tool
def look_up_drug(name: str) -> str:
    """Look up a drug on OpenFDA — returns brand/generic, indications, warnings,
    adverse reactions, and dosage. Use this when the user asks about a medication
    by name (e.g. 'side effects of metformin', 'what is amoxicillin used for')."""
    from .medical_kb import openfda_drug_label
    info = openfda_drug_label(name)
    if not info:
        return f"No OpenFDA drug record found for '{name}'."
    bits = [
        f"**{info['brand_name']}** (generic: {info['generic_name'] or '—'})",
        f"Manufacturer: {info['manufacturer'] or '—'}",
    ]
    if info.get('indications'):       bits.append(f"\n*Indications:* {info['indications']}")
    if info.get('warnings'):          bits.append(f"\n*Warnings:* {info['warnings']}")
    if info.get('adverse_reactions'): bits.append(f"\n*Adverse reactions:* {info['adverse_reactions']}")
    if info.get('dosage'):            bits.append(f"\n*Dosage:* {info['dosage']}")
    bits.append("\nSource: OpenFDA (US FDA drug label).")
    return "\n".join(bits)


@tool
def explain_condition(condition: str) -> str:
    """Patient-friendly explanation of a medical condition via MedlinePlus.
    Use this when the user asks 'what is X?' about a disease or diagnosis."""
    from .medical_kb import medlineplus_explain
    info = medlineplus_explain(condition)
    if not info:
        return f"No MedlinePlus entry found for '{condition}'."
    parts = [f"**{info['title']}** (ICD-10: {info['icd10']})"]
    if info.get('summary'): parts.append(info['summary'])
    if info.get('link'):    parts.append(f"\nMore: {info['link']}")
    parts.append("Source: MedlinePlus / NIH.")
    return "\n".join(parts)


@tool
def search_icd10(query: str) -> str:
    """Look up ICD-10 codes for a symptom or condition via NIH Clinical Tables.
    Use this to give clinicians the exact billable diagnosis code."""
    from .medical_kb import clinicaltables_search
    hits = clinicaltables_search(query, table='icd10cm', max_list=6)
    if not hits:
        return f"No ICD-10 codes matched '{query}'."
    return "ICD-10 candidates:\n" + "\n".join(
        f"  · {h['code']} — {h['display']}" for h in hits
    )


@tool
def medical_kb_search(query: str) -> str:
    """Search the curated SmartHospital clinical knowledge base for guidance on
    a condition: summary, symptoms, first-line steps, red flags. Use this BEFORE
    diagnosing or recommending treatment."""
    from .medical_kb import kb_search_remote
    hits = kb_search_remote(query, top_k=2)
    if not hits:
        return f"No KB entries matched '{query}'."
    blocks = []
    for h in hits:
        blocks.append(
            f"**{h['name']}** (ICD-10: {h.get('icd10', '—')})\n"
            f"{h.get('summary', '')}\n"
            f"*Symptoms:* {h.get('symptoms', '')}\n"
            f"*First steps:* {h.get('first_steps', '')}\n"
            f"*Red flags:* {h.get('red_flags', '')}"
        )
    return "\n\n".join(blocks)


@tool
def differential_diagnosis(symptoms: str, age: int = 35, sex: str = "male") -> str:
    """Run a differential-diagnosis pass on free-text symptoms via Infermedica.
    Returns ranked candidate conditions with probabilities, plus a triage
    urgency level. Requires INFERMEDICA_APP_ID/KEY in env — degrades gracefully
    if unavailable."""
    from .medical_kb import infermedica_parse, infermedica_diagnose, infermedica_triage
    parsed = infermedica_parse(symptoms, age=age, sex=sex)
    if parsed is None:
        return ("Differential diagnosis requires the Infermedica integration. "
                "Either it is not configured (INFERMEDICA_APP_ID/KEY missing) "
                "or the request failed. Falling back: please use medical_kb_search.")
    sym_ids = [m['id'] for m in parsed['present']]
    if not sym_ids:
        return f"Couldn't extract symptom concepts from: '{symptoms}'."
    diag = infermedica_diagnose(sym_ids, age=age, sex=sex) or {}
    triage = infermedica_triage(sym_ids, age=age, sex=sex) or {}
    lines = [
        "Recognised symptoms: " + ", ".join(m['name'] for m in parsed['present']),
        "",
        "Top conditions:",
    ]
    for c in diag.get('conditions', []):
        pct = int(c['probability'] * 100)
        lines.append(f"  · {c['name']} — {pct}%")
    if triage.get('description'):
        lines.append(f"\n**Triage:** {triage['description']} ({triage.get('level', '?')})")
    lines.append("\nSource: Infermedica. Not a substitute for clinical judgment.")
    return "\n".join(lines)


@tool
def count_records(kind: str = "all") -> str:
    """Quick counts. kind ∈ {patients, doctors, appointments, departments, predictions, all}.
    Use this for "how many X do we have" questions instead of listing them."""
    k = (kind or 'all').strip().lower()
    counts = {
        'patients':     Patient.objects.count(),
        'doctors':      Doctor.objects.count(),
        'appointments': Appointment.objects.count(),
        'departments':  Department.objects.count(),
        'predictions':  AIPrediction.objects.filter(status='SUCCESS').count(),
    }
    if k == 'all':
        return (
            f"Counts — patients: {counts['patients']}, doctors: {counts['doctors']}, "
            f"appointments: {counts['appointments']}, departments: {counts['departments']}, "
            f"successful AI predictions: {counts['predictions']}."
        )
    if k in counts:
        return f"{k.capitalize()}: {counts[k]}"
    return f"Unknown kind '{kind}'. Use one of: {', '.join(counts.keys())} or 'all'."


@tool
def get_today_snapshot() -> str:
    """Get TODAY's live snapshot: today's date, weekday, current local time, plus
    the number of appointments scheduled, completed, in-progress, and upcoming.

    Always prefer this tool when the user asks about "today", "right now",
    "this morning/afternoon", or anything time-relative. Do NOT guess the date.
    """
    from .models import DoctorTimeOff
    now = timezone.localtime()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    today_qs = Appointment.objects.filter(date_time__gte=today_start, date_time__lt=today_end)
    scheduled = today_qs.filter(status='Scheduled').count()
    completed = today_qs.filter(status='Completed').count()
    canceled  = today_qs.filter(status='Canceled').count()
    SESSION_MIN = 20
    in_progress = today_qs.filter(
        status='Scheduled',
        date_time__lte=now,
        date_time__gt=now - timedelta(minutes=SESSION_MIN),
    ).count()
    upcoming_today = today_qs.filter(status='Scheduled', date_time__gt=now).count()
    docs_off = DoctorTimeOff.objects.filter(date=today_start.date()).select_related('doctor')
    off_line = ''
    if docs_off.exists():
        names = ', '.join(o.doctor.name for o in docs_off[:5])
        off_line = f"\nDoctors off today: {docs_off.count()} ({names}{'…' if docs_off.count() > 5 else ''})."
    return (
        f"Today is {now.strftime('%A, %B %d, %Y')} (local time {now.strftime('%I:%M %p')}).\n"
        f"Total appointments today: {today_qs.count()} "
        f"(scheduled {scheduled}, completed {completed}, canceled {canceled}).\n"
        f"Right now: {in_progress} in progress, {upcoming_today} still upcoming today."
        f"{off_line}"
    )


@tool
def get_clinic_stats() -> str:
    """Get high-level clinic statistics: counts of doctors, patients, appointments, today's load, and AI coverage.

    Use this for dashboard-style overview questions like 'how busy are we today?' or 'how many patients do we have?'.
    """
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    today_qs = Appointment.objects.filter(date_time__gte=today_start, date_time__lt=today_end)
    total = Appointment.objects.count()
    analyzed = Appointment.objects.filter(predictions__status='SUCCESS').distinct().count()
    coverage = int(analyzed / total * 100) if total else 0
    return (
        f"Doctors: {Doctor.objects.count()}  |  Patients: {Patient.objects.count()}  |  "
        f"Total appointments: {total}\n"
        f"Today: {today_qs.count()} appointments "
        f"(scheduled {today_qs.filter(status='Scheduled').count()}, "
        f"completed {today_qs.filter(status='Completed').count()}).\n"
        f"AI coverage: {coverage}%."
    )


TOOLS_STAFF = [
    search_patients,
    list_all_patients,
    get_patient_summary,
    search_doctors,
    list_all_doctors,
    list_departments,
    list_appointments,
    get_ai_predictions,
    get_today_snapshot,
    get_clinic_stats,
    count_records,
    # Medical-knowledge tools (Phase 5.5)
    medical_kb_search,
    look_up_drug,
    explain_condition,
    search_icd10,
    differential_diagnosis,
]

TOOLS_PATIENT = [
    get_patient_summary,
    search_doctors,
    list_all_doctors,
    list_departments,
    list_appointments,
    get_ai_predictions,
    get_today_snapshot,
    # Knowledge tools — patients can ask "what is X" / drug info, but no
    # search_patients / count_records / clinic_stats.
    medical_kb_search,
    look_up_drug,
    explain_condition,
    differential_diagnosis,
]

# Backwards-compat alias used by tests + dispatch
TOOLS = TOOLS_STAFF
TOOL_MAP = {t.name: t for t in TOOLS_STAFF}


def _tools_for_session(session: ChatSession):
    """Return the tool list this user is allowed to invoke."""
    if session.user_id and not session.user.is_staff and hasattr(session.user, 'patient'):
        return TOOLS_PATIENT
    return TOOLS_STAFF


# ── System prompt + history rebuild ─────────────────────────────────────────


def _live_header() -> str:
    """A short live header prepended to every system prompt so the LLM always
    knows the current date/time, today's volume, and which doctors are off.

    Computed fresh on each turn — no caching — so we never drift.
    """
    from .models import DoctorTimeOff
    now = timezone.localtime()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    today_qs = Appointment.objects.filter(date_time__gte=today_start, date_time__lt=today_end)
    off_today = DoctorTimeOff.objects.filter(date=today_start.date()).count()
    return (
        f"=== LIVE CLINIC STATE ===\n"
        f"Today: {now.strftime('%A, %B %d, %Y')}   "
        f"Local time: {now.strftime('%I:%M %p %Z').strip()}\n"
        f"Today's appointments: {today_qs.count()} total "
        f"(scheduled {today_qs.filter(status='Scheduled').count()}, "
        f"completed {today_qs.filter(status='Completed').count()}, "
        f"canceled {today_qs.filter(status='Canceled').count()}). "
        f"Doctors off today: {off_today}.\n"
        f"When the user asks about 'today'/'tomorrow'/'this week', use THIS "
        f"date as ground truth. For deeper live numbers call get_today_snapshot.\n"
        f"========================\n\n"
    )


def _system_prompt(session: ChatSession) -> str:
    is_patient_user = bool(
        session.user_id and not session.user.is_staff
        and hasattr(session.user, 'patient') and session.user.patient is not None
    )

    if is_patient_user:
        p = session.user.patient
        base = (
            f"You are SmartHospital's patient assistant. You are speaking to "
            f"PATIENT #{p.pk} ({p.name}). You help them with THEIR own records "
            f"and answer general questions about the clinic and their health.\n\n"
            "TOOLS YOU MAY USE (limited set for patient privacy):\n"
            f"- get_patient_summary({p.pk}) — look up THIS patient's record\n"
            "- list_all_doctors() / search_doctors(query) — show available clinicians\n"
            "- list_departments() — list specialties\n"
            "- list_appointments(patient_id="
            f"{p.pk}, ...) — ONLY for this patient's own appointments\n"
            f"- get_ai_predictions(appointment_id) — only for this patient's own appointments\n"
            "- medical_kb_search(query) — curated clinical guidance (use BEFORE giving any health-info answer)\n"
            "- look_up_drug(name) — OpenFDA drug label info\n"
            "- explain_condition(name) — patient-friendly MedlinePlus explanation\n"
            "- differential_diagnosis(symptoms, age, sex) — symptom triage when patient describes complaints\n\n"
            "STRICT RULES:\n"
            "1. NEVER call tools related to other patients (search_patients, list_all_patients, count_records, get_clinic_stats).\n"
            f"2. NEVER reveal information about other patients. Refuse: 'I can only access your own record (#{p.pk}).'\n"
            "3. NEVER give a definitive diagnosis or prescription. Use the medical knowledge tools to inform, then ALWAYS recommend booking with a qualified doctor.\n"
            f"4. When listing appointments, always pass patient_id={p.pk}.\n"
            "5. Be warm, concise, reassuring. Translate medical jargon into plain language.\n"
            "6. Cite IDs ('appointment #12', 'doctor #3') and tool sources ('per OpenFDA', 'per MedlinePlus').\n"
        )
    else:
        base = (
            "You are SmartHospital's clinical assistant for STAFF — concise, professional, warm.\n\n"
            "RECORDS TOOLS:\n"
            "- search_patients(query) / list_all_patients(page) — find or enumerate patients\n"
            "- get_patient_summary(patient_id) — full record for one patient\n"
            "- search_doctors(query) / list_all_doctors() — find or enumerate doctors\n"
            "- list_departments() — clinic departments with doctor counts\n"
            "- list_appointments(patient_id, doctor_id, days, status) — filtered list\n"
            "- get_ai_predictions(appointment_id) — AI diagnosis history for one appt\n"
            "- get_clinic_stats() — today's load + AI coverage overview\n"
            "- count_records(kind) — quick counts\n\n"
            "MEDICAL-KNOWLEDGE TOOLS (use these to support clinical reasoning):\n"
            "- medical_kb_search(query) — curated SmartHospital KB: summary, symptoms, first-line, red flags\n"
            "- look_up_drug(name) — OpenFDA drug label (indications, warnings, dosage)\n"
            "- explain_condition(name) — MedlinePlus patient-friendly explanation\n"
            "- search_icd10(query) — NIH ICD-10 code lookup for billable diagnosis\n"
            "- differential_diagnosis(symptoms, age, sex) — Infermedica triage + ranked candidates\n\n"
            "RULES:\n"
            "1. ALWAYS call a tool for data questions. Never guess IDs, names, counts, or drug facts.\n"
            "2. For 'list all' / 'show me everyone', use list_all_*. For 'how many', use count_records.\n"
            "3. For diagnostic / treatment / drug questions, ALWAYS first call medical_kb_search and/or look_up_drug; cite the source in your reply.\n"
            "4. Cite IDs ('patient #12', 'appointment #4', 'doctor #3') so the UI auto-links them.\n"
            "5. Format multi-item answers as Markdown lists. Keep prose under ~140 words.\n"
            "6. Never give a definitive diagnosis or prescription as YOU — present what the tools returned and recommend clinical confirmation.\n"
            "7. If a tool returns no results, say so plainly — don't fabricate.\n"
            "8. When summarizing a patient or doctor, include their ID, key facts, and one suggested next step.\n"
        )

    kind = session.page_context_kind
    cid  = session.page_context_id
    if kind == 'patient' and cid:
        try:
            p = Patient.objects.get(pk=cid)
            base += (
                f"\nCONTEXT: The user is currently viewing patient #{p.pk} "
                f"({p.name}). When they say 'this patient' / 'her' / 'him', "
                f"refer to this patient. Call get_patient_summary({p.pk}) "
                f"early in the conversation if needed."
            )
        except Patient.DoesNotExist:
            pass
    elif kind == 'appointment' and cid:
        try:
            a = Appointment.objects.select_related('patient', 'doctor').get(pk=cid)
            base += (
                f"\nCONTEXT: The user is currently viewing appointment #{a.pk}: "
                f"{a.patient.name} with Dr. {a.doctor.name} on "
                f"{a.date_time:%Y-%m-%d %H:%M}. When they say 'this appointment', "
                f"refer to it. Call get_ai_predictions({a.pk}) if they ask "
                f"about the AI diagnosis."
            )
        except Appointment.DoesNotExist:
            pass
    elif kind == 'dashboard':
        base += (
            "\nCONTEXT: The user is on the dashboard. For overview questions, "
            "call get_clinic_stats."
        )
    return _live_header() + base


def _hydrate_history(session: ChatSession) -> list:
    """Rebuild the message list for the LLM from saved ChatMessages."""
    msgs: list = [SystemMessage(content=_system_prompt(session))]
    recent = list(session.messages.order_by('created_at'))[-HISTORY_LIMIT:]
    for m in recent:
        if m.role == 'user':
            msgs.append(HumanMessage(content=m.content))
        elif m.role == 'assistant':
            tool_calls = []
            if m.tool_calls_json:
                try:
                    tool_calls = json.loads(m.tool_calls_json)
                except (ValueError, TypeError):
                    tool_calls = []
            msgs.append(AIMessage(content=m.content or '', tool_calls=tool_calls))
        elif m.role == 'tool':
            msgs.append(ToolMessage(content=m.content, tool_call_id=m.tool_call_id))
    return msgs


def _content_text(ai: AIMessage) -> str:
    """Extract plain text from an AIMessage whose content may be str or list-of-blocks."""
    c = ai.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(
            block.get('text', '') if isinstance(block, dict) else str(block)
            for block in c
        )
    return str(c or '')


# ── Public entry point ─────────────────────────────────────────────────────


def respond(session: ChatSession, user_text: str) -> dict[str, Any]:
    """Run one chat turn: persist the user message, invoke the tool loop, persist all replies.

    Returns a dict with the final assistant reply, total tokens, and latency.
    """
    user_text = (user_text or '').strip()
    if not user_text:
        raise ValueError("empty message")

    ChatMessage.objects.create(session=session, role='user', content=user_text)
    if not session.title:
        session.title = user_text[:80]
    session.save()  # bumps updated_at

    llm = ChatOpenAI(
        model=MODEL,
        api_key=settings.LIGHTNING_API_KEY,
        base_url=BASE_URL,
        temperature=0.4,
        max_tokens=600,
    )
    allowed_tools = _tools_for_session(session)
    allowed_tool_map = {t.name: t for t in allowed_tools}
    llm_with_tools = llm.bind_tools(allowed_tools)

    messages = _hydrate_history(session)
    total_tokens = 0
    final_text = ''
    started = time.time()

    for _ in range(MAX_TOOL_ITERS):
        ai = llm_with_tools.invoke(messages)
        usage = (getattr(ai, 'usage_metadata', None) or {})
        total_tokens += usage.get('total_tokens', 0) or 0
        messages.append(ai)

        tool_calls = list(getattr(ai, 'tool_calls', None) or [])
        text_part = _content_text(ai)

        ChatMessage.objects.create(
            session=session,
            role='assistant',
            content=text_part,
            tool_calls_json=json.dumps(tool_calls) if tool_calls else '',
            total_tokens=usage.get('total_tokens', 0) or 0,
        )

        if not tool_calls:
            final_text = text_part
            break

        for tc in tool_calls:
            name = tc.get('name')
            tc_id = tc.get('id') or ''
            tool_obj = allowed_tool_map.get(name)
            if tool_obj is None:
                result_text = f"Tool '{name}' is not available."
                tool_msg = ToolMessage(content=result_text, tool_call_id=tc_id)
            else:
                try:
                    tool_msg = tool_obj.invoke(tc)
                except Exception as exc:
                    tool_msg = ToolMessage(
                        content=f"Tool error: {exc}",
                        tool_call_id=tc_id,
                    )
            messages.append(tool_msg)
            ChatMessage.objects.create(
                session=session,
                role='tool',
                content=str(tool_msg.content)[:4000],
                tool_call_id=tc_id,
                tool_name=name or '',
            )
    else:
        final_text = (
            "I hit the tool-call limit while looking that up. "
            "Could you ask a more specific question?"
        )
        ChatMessage.objects.create(
            session=session,
            role='assistant',
            content=final_text,
        )

    latency_ms = int((time.time() - started) * 1000)
    return {
        'reply':        final_text or "(no reply)",
        'total_tokens': total_tokens,
        'latency_ms':   latency_ms,
    }
