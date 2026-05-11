"""WebSocket consumers for live dashboard updates.

Authenticated clients connect to /ws/updates/ and join the 'updates' group.
The audit signal broadcasts events to that group via channel_layer.group_send.
"""

import json

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer

UPDATES_GROUP = 'updates'


class UpdatesConsumer(WebsocketConsumer):
    def connect(self):
        user = self.scope.get('user')
        if user is None or not user.is_authenticated:
            self.close(code=4001)
            return
        async_to_sync(self.channel_layer.group_add)(UPDATES_GROUP, self.channel_name)
        self.accept()
        self.send(text_data=json.dumps({'type': 'hello', 'user': user.username}))

    def disconnect(self, code):
        async_to_sync(self.channel_layer.group_discard)(UPDATES_GROUP, self.channel_name)

    # Receiver for messages broadcast to the 'updates' group
    def audit_event(self, event):
        self.send(text_data=json.dumps({
            'type':       'audit',
            'action':     event.get('action'),
            'kind':       event.get('kind'),
            'target_id':  event.get('target_id'),
            'summary':    event.get('summary'),
            'user':       event.get('user'),
            'created_at': event.get('created_at'),
        }))
