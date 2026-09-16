from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from cs_duty.database import ROOT
from cs_duty.models import validate_profile

DEFAULTS = {
    'transport': 'mock', 'shop': 'local-shop', 'mode': 'draft',
    'mock_url': 'http://127.0.0.1:18766/workbench', 'jd_url': 'https://dongdong.jd.com/',
    'channel': 'msedge', 'poll_seconds': 2, 'merge_seconds': 2, 'max_sessions': 100,
    'custom_fields': ['product', 'requirements', 'quantity', 'deadline', 'contact'],
    'feishu_webhook': '', 'feishu_secret': '', 'feishu_enabled': False,
    'feishu_mode': 'webhook', 'feishu_app_id': '', 'feishu_app_secret': '',
    'feishu_chat_id': '', 'feishu_allowed_users': [], 'feishu_allowed_chats': [],
    'feishu_allow_private': False,
}
FIELD_LABELS = {'product': '产品型号', 'requirements': '定制内容', 'quantity': '数量',
                'deadline': '期望交期', 'contact': '联系方式', 'company': '公司或称呼'}


class Settings:
    def __init__(self, db, env_path: Path | None = ROOT / '.env'):
        self.db = db
        if not db.setting('runtime'):
            db.set_setting('runtime', DEFAULTS)
        if not db.rows('SELECT id FROM profiles') and env_path and env_path.exists():
            env = dotenv_values(env_path)
            if env.get('MINIMAX_API_KEY'):
                pid = self.save_profile({'name': 'MiniMax 主模型', 'protocol': 'anthropic',
                    'base_url': env.get('MINIMAX_BASE_URL', 'https://api.minimax.cn/anthropic'),
                    'model': env.get('MINIMAX_MODEL', 'MiniMax-M3'), 'api_key': env['MINIMAX_API_KEY']})
                db.set_setting('active_profile', pid)

    def runtime(self, public=False):
        config = {**DEFAULTS, **self.db.setting('runtime', {})}
        if public:
            config['has_feishu_webhook'] = bool(config.pop('feishu_webhook', ''))
            config['has_feishu_secret'] = bool(config.pop('feishu_secret', ''))
            config['has_feishu_app_secret'] = bool(config.pop('feishu_app_secret', ''))
        return config

    def save_runtime(self, data):
        config = self.runtime()
        for key in DEFAULTS:
            if key in data:
                if key in ('feishu_webhook', 'feishu_secret', 'feishu_app_secret') and not data[key]:
                    continue
                config[key] = data[key]
        if config['transport'] not in ('mock', 'jingmai') or config['mode'] not in ('draft', 'auto'):
            raise ValueError('无效运行模式')
        if not isinstance(config['shop'], str) or not config['shop'].strip() or len(config['shop']) > 80:
            raise ValueError('请填写店铺标识')
        for key in ('poll_seconds', 'merge_seconds', 'max_sessions'):
            config[key] = int(config[key])
        if not 1 <= config['poll_seconds'] <= 60 or not 0 <= config['merge_seconds'] <= 20 or not 1 <= config['max_sessions'] <= 500:
            raise ValueError('轮询 1–60 秒、合并等待 0–20 秒、每轮会话上限 1–500')
        if config['channel'] not in ('msedge', 'chromium', 'chrome'):
            raise ValueError('无效浏览器类型')
        mock = urlparse(config['mock_url'])
        if mock.scheme != 'http' or mock.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('模拟页面必须是本机 HTTP 地址')
        real = urlparse(config['jd_url'])
        if real.scheme != 'https' or real.hostname != 'dongdong.jd.com':
            raise ValueError('京东适配目前仅支持 https://dongdong.jd.com/')
        if not isinstance(config['custom_fields'], list) or not config['custom_fields'] or any(f not in FIELD_LABELS for f in config['custom_fields']):
            raise ValueError('定制收集字段无效')
        if config['feishu_webhook']:
            hook = urlparse(config['feishu_webhook'])
            if hook.scheme != 'https' or hook.hostname not in ('open.feishu.cn', 'open.larksuite.com') or not hook.path.startswith('/open-apis/bot/v2/hook/'):
                raise ValueError('请填写飞书自定义机器人 Webhook 地址')
        if not isinstance(config['feishu_enabled'], bool):
            raise ValueError('通知开关无效')
        if config['feishu_mode'] not in ('webhook', 'app') or not isinstance(config['feishu_allow_private'], bool):
            raise ValueError('飞书接入方式无效')
        for key, prefix in (('feishu_allowed_users', 'ou_'), ('feishu_allowed_chats', 'oc_')):
            values = config[key]
            if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, str) or not v.startswith(prefix) or not v.replace('_', '').isalnum() for v in values):
                raise ValueError('请填写有效的飞书用户或群聊 ID 列表')
            config[key] = list(dict.fromkeys(values))
        for key in ('feishu_app_id', 'feishu_app_secret', 'feishu_chat_id'):
            if not isinstance(config[key], str) or len(config[key]) > 256:
                raise ValueError('飞书应用配置格式错误')
            config[key] = config[key].strip()
        if config['feishu_mode'] == 'app' and config['feishu_enabled']:
            if not config['feishu_app_id'].startswith('cli_') or not config['feishu_app_secret']:
                raise ValueError('请先填写飞书 App ID 和 App Secret')
            if config['feishu_chat_id'] not in config['feishu_allowed_chats'] or not config['feishu_allowed_users']:
                raise ValueError('请将通知会话加入允许列表，并至少指定一位允许的员工')
        self.db.set_setting('runtime', config)

    def profiles(self):
        rows = self.db.rows('SELECT id,name,protocol,base_url,model,timeout,max_tokens,api_key<>\'\' AS has_key FROM profiles ORDER BY name')
        return {'items': rows, 'active': self.db.setting('active_profile', '')}

    def profile(self, pid=None):
        profile = self.db.one('SELECT * FROM profiles WHERE id=?', (pid or self.db.setting('active_profile', ''),))
        if not profile:
            raise ValueError('请先保存并启用一个模型配置')
        return profile

    def save_profile(self, data):
        valid = validate_profile(data)
        pid = str(data.get('id') or uuid.uuid4().hex)
        old = self.db.one('SELECT * FROM profiles WHERE id=?', (pid,))
        key = str(data.get('api_key') or (old or {}).get('api_key') or '').strip()
        if not key or len(key) > 4096 or any(c.isspace() for c in key):
            raise ValueError('请填写有效 API Key')
        self.db.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?,?,?,?,?,?)',
            (pid, valid['name'], valid['protocol'], valid['base_url'], key, valid['model'], valid['timeout'], valid['max_tokens']))
        if not self.db.setting('active_profile'):
            self.db.set_setting('active_profile', pid)
        return pid
