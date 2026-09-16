"""Owned SDK process. Credentials arrive on stdin; stdout carries only bounded IPC."""
import json
import logging
import sys


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    config = json.loads(sys.stdin.readline())
    import lark_oapi as lark
    from lark_oapi.core.log import logger

    class StatusLog(logging.Handler):
        def emit(self, record):
            text = record.getMessage()
            if 'disconnected to ' in text:
                emit({'kind': 'status', 'state': 'reconnecting'})
            elif 'connected to ' in text:
                emit({'kind': 'status', 'state': 'connected'})

    logger.handlers.clear()
    logger.addHandler(StatusLog())
    logger.propagate = False

    def receive(data):
        event, header = data.event, data.header
        message, sender = event.message, event.sender
        value = {'kind': 'message', 'event_id': header.event_id,
                 'message_id': message.message_id, 'chat_id': message.chat_id,
                 'chat_type': message.chat_type, 'message_type': message.message_type,
                 'content': message.content, 'parent_id': message.parent_id or '',
                 'sender_id': sender.sender_id.open_id, 'sender_type': sender.sender_type,
                 'mentions': [{'key': m.key, 'id': m.id.open_id} for m in message.mentions or []]}
        if len(json.dumps(value)) > 100000:
            return
        emit(value)
        # SDK acknowledges the event only after the parent has durably handled it.
        if sys.stdin.readline().strip() != 'ok':
            raise RuntimeError('Local event handling did not complete')

    handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(receive).build()
    client = lark.ws.Client(config['app_id'], config['app_secret'], event_handler=handler, log_level=lark.LogLevel.INFO)
    client.on_reconnecting = lambda: emit({'kind': 'status', 'state': 'reconnecting'})
    try:
        client.start()
    except Exception:
        emit({'kind': 'status', 'state': 'error'})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
