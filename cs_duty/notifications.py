"""Explicitly enabled colleague notifications. Unknown outcomes are not retried blindly."""
import base64
import hashlib
import hmac
import json
import time
import urllib.request

from cs_duty.models import NoRedirect


def notify_task(db, settings, task):
    config = settings.runtime()
    if not config['feishu_enabled'] or not config['feishu_webhook'] or task['notification'] != 'pending':
        return
    if not db.execute("UPDATE tasks SET notification='sending' WHERE id=? AND notification='pending'", (task['id'],)):
        return
    conversation = db.conversation(task['conversation_id'])
    content = '\n'.join([
        '客服协同待办 ' + task['id'][:8],
        '客户：' + conversation['name'], '店铺：' + conversation['shop'],
        '问题：' + task['summary'], '已收集信息：' + task['fields'],
        '请在客服管理页面填写处理结果。',
    ])
    body = {'msg_type': 'text', 'content': {'text': content}}
    if config['feishu_secret']:
        timestamp = str(int(time.time()))
        signing_key = (timestamp + '\n' + config['feishu_secret']).encode()
        body.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(signing_key, b'', hashlib.sha256).digest()).decode())
    try:
        request = urllib.request.Request(config['feishu_webhook'], data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            result = json.load(response)
        if result.get('code', result.get('StatusCode', -1)) != 0:
            status = 'failed'
        else:
            status = 'sent'
    except Exception:
        status = 'uncertain'
    db.execute('UPDATE tasks SET notification=? WHERE id=?', (status, task['id']))
