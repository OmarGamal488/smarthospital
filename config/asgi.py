"""ASGI config for config project. Routes HTTP to Django and WebSocket to Channels."""

import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.core.asgi import get_asgi_application
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

from hospital.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    'http':      get_asgi_application(),
    'websocket': AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            URLRouter(websocket_urlpatterns),
        ),
    ),
})
