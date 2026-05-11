"""Patient-facing Telegram bot.

Lets a patient book SmartHospital appointments without opening the website.
Flow:
    1. Patient visits /patient/profile/ on the web → copies a one-time link code.
    2. In Telegram they message the bot `/start`, then `/link <code>`.
    3. Once linked, all bot commands act on that patient's record.

Commands:
    /start            — welcome + (if linked) menu shortcut
    /link CODE        — claim this Telegram account for the SmartHospital patient with this code
    /doctors          — list bookable doctors (one button per doctor)
    /book             — start a booking flow (department → doctor → day → slot → reason)
    /mybookings       — list this patient's upcoming + recent visits
    /cancel           — abort a multi-step flow
    /help             — show all commands

Implementation notes:
    - python-telegram-bot v22, async.
    - Uses `application.run_polling()` so no public webhook is needed.
    - Django ORM calls are wrapped in sync_to_async (the patterns are simple
      enough that we don't need bulk_create or transaction.on_commit hooks).
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import date, datetime, timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

log = logging.getLogger("hospital.telegram")

# Conversation flow state keys
F_DEPT, F_DOCTOR, F_DATE, F_SLOT, F_REASON = range(5)


def _new_code(length: int = 8) -> str:
    """Generate a short alphanumeric link code (avoiding ambiguous chars)."""
    alphabet = string.ascii_uppercase.replace('O', '').replace('I', '') + '23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


@sync_to_async
def _get_patient_by_chat(chat_id: int):
    """Return the Patient linked to this Telegram chat, or None."""
    from .models import TelegramLink
    link = (TelegramLink.objects
            .select_related('user', 'user__patient')
            .filter(chat_id=chat_id, linked_at__isnull=False)
            .first())
    if not link or not hasattr(link.user, 'patient'):
        return None
    return link.user.patient


@sync_to_async
def _claim_link(chat_id: int, code: str, tg_username: str):
    """Pair a Telegram chat with the SmartHospital user whose link_code matches.

    Returns the patient's name on success, or an error message string starting
    with '!'.
    """
    from .models import TelegramLink
    code = (code or '').strip().upper()
    if not code:
        return '!Please send the code shown on your profile page.'
    # Re-using an existing chat — friendly message
    existing = TelegramLink.objects.filter(chat_id=chat_id).first()
    if existing and existing.linked_at:
        return f'!You are already linked as {existing.user.username}.'
    target = TelegramLink.objects.filter(link_code=code, linked_at__isnull=True).first()
    if target is None:
        return '!That code is invalid or already used. Open your profile and generate a new one.'
    target.chat_id   = chat_id
    target.username  = tg_username or ''
    target.linked_at = timezone.now()
    target.save(update_fields=['chat_id', 'username', 'linked_at'])
    name = target.user.patient.name if hasattr(target.user, 'patient') else target.user.username
    return name


@sync_to_async
def _list_departments():
    from .models import Department
    return list(Department.objects.order_by('name').values('id', 'name'))


@sync_to_async
def _doctors_in_department(dept_id: int):
    from .models import Doctor
    return list(Doctor.objects.filter(department_id=dept_id)
                              .order_by('name')
                              .values('id', 'name', 'specialty'))


@sync_to_async
def _next_days_for_doctor(doctor_id: int):
    from .booking import next_available_days
    from .models import Doctor
    doc = Doctor.objects.filter(pk=doctor_id).first()
    if not doc:
        return []
    return [d.isoformat() for d in next_available_days(doc, n=7)]


@sync_to_async
def _slots_for_doctor_on(doctor_id: int, iso_date: str):
    from .booking import available_slots
    from .models import Doctor
    doc = Doctor.objects.filter(pk=doctor_id).first()
    if not doc:
        return []
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        return []
    return [s.isoformat() for s in available_slots(doc, d)]


@sync_to_async
def _create_appointment(chat_id: int, doctor_id: int, slot_iso: str, reason: str):
    from .ai_service import predict_diagnosis
    from .booking import available_slots
    from .models import AIPrediction, Appointment, Doctor, TelegramLink

    link = (TelegramLink.objects
            .select_related('user', 'user__patient')
            .filter(chat_id=chat_id, linked_at__isnull=False)
            .first())
    if not link or not hasattr(link.user, 'patient'):
        return None, 'You are not linked to a patient account.'
    doc = Doctor.objects.filter(pk=doctor_id).first()
    if not doc:
        return None, 'That doctor is no longer available.'
    try:
        slot_dt = datetime.fromisoformat(slot_iso)
    except ValueError:
        return None, 'Bad slot.'
    if timezone.is_naive(slot_dt):
        slot_dt = timezone.make_aware(slot_dt, timezone.get_current_timezone())
    # Race-condition check
    if slot_dt not in available_slots(doc, slot_dt.date()):
        return None, 'That slot was just taken. Try /book again.'
    appt = Appointment.objects.create(
        doctor=doc, patient=link.user.patient,
        date_time=slot_dt, reason=reason or 'Booked via Telegram',
        status='Scheduled',
    )
    try:
        result = predict_diagnosis(appt)
        AIPrediction.objects.create(appointment=appt, **result)
    except Exception:
        pass
    return appt.pk, (
        f'Appointment #{appt.pk} confirmed with Dr. {doc.name} '
        f'on {slot_dt.strftime("%A, %b %d at %I:%M %p")}.'
    )


@sync_to_async
def _list_appointments_for_chat(chat_id: int):
    from .models import Appointment, TelegramLink
    link = (TelegramLink.objects.select_related('user', 'user__patient')
            .filter(chat_id=chat_id, linked_at__isnull=False).first())
    if not link or not hasattr(link.user, 'patient'):
        return None
    now = timezone.now()
    qs = (Appointment.objects.select_related('doctor', 'doctor__department')
                              .filter(patient=link.user.patient)
                              .order_by('-date_time'))
    out = []
    for a in qs[:10]:
        out.append({
            'pk':     a.pk,
            'when':   timezone.localtime(a.date_time).strftime('%a, %b %d · %I:%M %p'),
            'doctor': a.doctor.name,
            'dept':   a.doctor.department.name,
            'status': a.status,
            'is_upcoming': a.date_time >= now,
        })
    return out


# ── Telegram handlers ────────────────────────────────────────────────────────


def build_application():
    """Wire up the python-telegram-bot Application with all handlers."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        Application, CommandHandler, ConversationHandler, ContextTypes,
        CallbackQueryHandler, MessageHandler, filters,
    )

    token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '') or ''
    if not token:
        raise RuntimeError(
            'TELEGRAM_BOT_TOKEN is not set. Get a token from @BotFather, '
            'then export TELEGRAM_BOT_TOKEN=... before running `manage.py runbot`.'
        )

    # ── command handlers ─────────────────────────────────────────────
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        patient = await _get_patient_by_chat(update.effective_chat.id)
        if patient is None:
            await update.message.reply_text(
                "Hi! I'm the SmartHospital booking bot.\n\n"
                "First link your patient account:\n"
                "  1. Sign in at the web portal and open /patient/profile/.\n"
                "  2. Copy the *Telegram link code*.\n"
                "  3. Send me:  /link YOURCODE\n\n"
                "Then you can use /doctors, /book, /mybookings.",
                parse_mode='Markdown',
            )
            return
        await update.message.reply_text(
            f"Hi {patient.name.split()[0]}! You're linked.\n"
            "Commands:\n"
            "  /book — schedule a new visit\n"
            "  /mybookings — your appointments\n"
            "  /doctors — browse clinicians\n"
            "  /help"
        )

    async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args or []
        code = args[0] if args else ''
        msg = await _claim_link(update.effective_chat.id, code,
                                update.effective_user.username or '')
        if msg.startswith('!'):
            await update.message.reply_text(msg.lstrip('!'))
        else:
            await update.message.reply_text(
                f"Linked! Hi {msg}. Try /book to schedule a visit."
            )

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "SmartHospital bot — commands:\n"
            "  /start — welcome\n"
            "  /link CODE — pair this chat with your patient account\n"
            "  /doctors — list clinicians\n"
            "  /book — start a booking\n"
            "  /mybookings — your visits\n"
            "  /cancel — abort a booking flow"
        )

    async def doctors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        depts = await _list_departments()
        if not depts:
            await update.message.reply_text("No departments yet.")
            return
        lines = ["*Browse by department*"]
        for d in depts:
            docs = await _doctors_in_department(d['id'])
            lines.append(f"\n_{d['name']}_ ({len(docs)})")
            for doc in docs[:6]:
                lines.append(f"  • Dr. {doc['name']} — {doc['specialty']}")
        lines.append("\nUse /book to schedule.")
        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

    async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
        items = await _list_appointments_for_chat(update.effective_chat.id)
        if items is None:
            await update.message.reply_text(
                "You're not linked yet. Send /link YOURCODE (get the code from /patient/profile/)."
            )
            return
        if not items:
            await update.message.reply_text("You have no appointments yet. Use /book to schedule one.")
            return
        lines = ["*Your appointments*"]
        for a in items:
            tag = '🟢 upcoming' if a['is_upcoming'] and a['status'] == 'Scheduled' else f"⚪ {a['status'].lower()}"
            lines.append(f"\n  #{a['pk']} · *{a['when']}*\n  Dr. {a['doctor']} ({a['dept']}) · {tag}")
        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

    # ── booking conversation ─────────────────────────────────────────
    async def book_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
        patient = await _get_patient_by_chat(update.effective_chat.id)
        if patient is None:
            await update.message.reply_text(
                "You're not linked yet. Send /link YOURCODE first."
            )
            return ConversationHandler.END
        depts = await _list_departments()
        if not depts:
            await update.message.reply_text("No clinics yet — try later.")
            return ConversationHandler.END
        kb = [[InlineKeyboardButton(d['name'], callback_data=f"dept:{d['id']}")] for d in depts]
        await update.message.reply_text(
            f"Hi {patient.name.split()[0]}! Step 1/4 — choose a department:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return F_DEPT

    async def pick_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        dept_id = int(q.data.split(':', 1)[1])
        doctors = await _doctors_in_department(dept_id)
        if not doctors:
            await q.edit_message_text("No doctors in that department. /book to retry.")
            return ConversationHandler.END
        kb = [
            [InlineKeyboardButton(f"Dr. {d['name']} — {d['specialty']}", callback_data=f"doc:{d['id']}")]
            for d in doctors[:10]
        ]
        await q.edit_message_text("Step 2/4 — choose a doctor:",
                                  reply_markup=InlineKeyboardMarkup(kb))
        return F_DOCTOR

    async def pick_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        doc_id = int(q.data.split(':', 1)[1])
        context.user_data['doctor_id'] = doc_id
        days = await _next_days_for_doctor(doc_id)
        if not days:
            await q.edit_message_text("That doctor has no openings in the next two weeks.")
            return ConversationHandler.END
        kb = []
        for d in days[:7]:
            label = date.fromisoformat(d).strftime('%a, %b %d')
            kb.append([InlineKeyboardButton(label, callback_data=f"day:{d}")])
        await q.edit_message_text("Step 3/4 — choose a day:",
                                  reply_markup=InlineKeyboardMarkup(kb))
        return F_DATE

    async def pick_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        day = q.data.split(':', 1)[1]
        slots = await _slots_for_doctor_on(context.user_data['doctor_id'], day)
        if not slots:
            await q.edit_message_text("No slots on that day. /book to pick another.")
            return ConversationHandler.END
        kb = []
        # 2 columns of slot chips, up to 12 chips
        row: list = []
        for s in slots[:12]:
            label = datetime.fromisoformat(s).strftime('%I:%M %p')
            row.append(InlineKeyboardButton(label, callback_data=f"slot:{s}"))
            if len(row) == 2:
                kb.append(row); row = []
        if row:
            kb.append(row)
        await q.edit_message_text("Step 4/4 — choose a time:",
                                  reply_markup=InlineKeyboardMarkup(kb))
        return F_SLOT

    async def ask_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        context.user_data['slot'] = q.data.split(':', 1)[1]
        await q.edit_message_text(
            "Finally — describe the reason for the visit in a few words.\n"
            "(e.g. *headache, dizziness for 3 days*). Send /cancel to abort.",
            parse_mode='Markdown',
        )
        return F_REASON

    async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
        reason = (update.message.text or '').strip()
        pk, msg = await _create_appointment(
            update.effective_chat.id,
            context.user_data['doctor_id'],
            context.user_data['slot'],
            reason,
        )
        await update.message.reply_text(msg + ("\n\nUse /mybookings any time." if pk else ""))
        return ConversationHandler.END

    async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Cancelled. Use /book to start over.")
        return ConversationHandler.END

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler('start',       start))
    app.add_handler(CommandHandler('help',        help_cmd))
    app.add_handler(CommandHandler('link',        link_cmd))
    app.add_handler(CommandHandler('doctors',     doctors_cmd))
    app.add_handler(CommandHandler('mybookings',  my_bookings))

    booking = ConversationHandler(
        entry_points=[CommandHandler('book', book_entry)],
        states={
            F_DEPT:   [CallbackQueryHandler(pick_doctor, pattern=r'^dept:')],
            F_DOCTOR: [CallbackQueryHandler(pick_date,   pattern=r'^doc:')],
            F_DATE:   [CallbackQueryHandler(pick_slot,   pattern=r'^day:')],
            F_SLOT:   [CallbackQueryHandler(ask_reason,  pattern=r'^slot:')],
            F_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler('cancel', cancel_flow)],
    )
    app.add_handler(booking)
    return app
