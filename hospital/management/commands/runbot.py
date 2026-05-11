"""Run the SmartHospital patient-booking Telegram bot in long-polling mode.

Usage:
    export TELEGRAM_BOT_TOKEN=...   # from @BotFather
    python manage.py runbot

The bot pairs with web patient accounts via a one-time code shown on
/patient/profile/. See hospital/telegram_bot.py for the command list.
"""

import logging
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the patient Telegram bot (long-polling).'

    def handle(self, *args, **opts):
        # Bot module imports python-telegram-bot lazily so the rest of the
        # project still works without the dep being installed.
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        )
        from hospital.telegram_bot import build_application
        app = build_application()
        self.stdout.write(self.style.SUCCESS(
            'SmartHospital bot is online. Press Ctrl+C to stop.'))
        # `run_polling` is sync — it spins up its own asyncio loop.
        app.run_polling(allowed_updates=['message', 'callback_query'])
