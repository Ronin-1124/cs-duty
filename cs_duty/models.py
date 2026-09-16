"""Small protocol adapters; only public final text reaches the workflow."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


class ModelError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_profile(data):
    protocol = data.get('protocol')
    if protocol not in ('anthropic', 'openai'):
        raise ValueError('请选择 Anthropic 或 OpenAI 兼容协议')
    url = str(data.get('base_url', '')).strip().rstrip('/')
    parsed = urlparse(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('API 地址格式无效')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('远程 API 请使用 HTTPS')
    name, model = str(data.get('name', '')).strip(), str(data.get('model', '')).strip()
    if not name or not model or len(name) > 100 or len(model) > 200:
        raise ValueError('请填写配置名称和模型名称')
    timeout, max_tokens = int(data.get('timeout', 40)), int(data.get('max_tokens', 1800))
    if not 5 <= timeout <= 120 or not 128 <= max_tokens <= 8192:
        raise ValueError('超时范围 5–120 秒，输出长度范围 128–8192')
    return dict(name=name, protocol=protocol, base_url=url, model=model, timeout=timeout, max_tokens=max_tokens)


class ModelClient:
    def __init__(self, profile):
        self.profile = profile

    def complete(self, system, user):
        p = self.profile
        if not p.get('api_key'):
            raise ModelError('尚未配置 API Key')
        body = {'model': p['model'], 'max_tokens': p['max_tokens']}
        if p['model'].startswith('MiniMax-M3'):
            body['thinking'] = {'type': 'disabled'}
        headers = {'Content-Type': 'application/json'}
        if p['protocol'] == 'anthropic':
            suffix = '/messages' if p['base_url'].endswith('/v1') else '/v1/messages'
            body.update(system=system, messages=[{'role': 'user', 'content': user}])
            headers.update({'x-api-key': p['api_key'], 'anthropic-version': '2023-06-01'})
        else:
            suffix = '/chat/completions'
            body['messages'] = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
            if p['model'].startswith('MiniMax-'):
                body['reasoning_split'] = True
            headers['Authorization'] = 'Bearer ' + p['api_key']
        request = urllib.request.Request(p['base_url'] + suffix, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=p['timeout']) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            messages = {401: '认证失败，请检查密钥与 API 区域', 403: '接口拒绝访问', 429: '请求限流或额度不足'}
            raise ModelError(messages.get(exc.code, f'模型接口返回 HTTP {exc.code}')) from None
        except (OSError, ValueError):
            raise ModelError('模型接口连接失败、超时或响应格式错误') from None
        if data.get('error') or (data.get('base_resp') or {}).get('status_code', 0):
            raise ModelError('模型服务返回业务错误，请检查账户与接口配置')
        if p['protocol'] == 'anthropic':
            text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
        else:
            choices = data.get('choices') or [{}]
            text = (choices[0].get('message') or {}).get('content') or ''
        if not isinstance(text, str):
            raise ModelError('模型未返回文本')
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()
        if not text or '<think>' in text:
            raise ModelError('模型未返回完整答复')
        return text

    def plan(self, system, context):
        text = self.complete(system, json.dumps(context, ensure_ascii=False))
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
        try:
            value = json.loads(text)
        except ValueError:
            raise ModelError('模型返回的处理方案不是有效 JSON，本轮未发送') from None
        if not isinstance(value, dict):
            raise ModelError('模型处理方案格式错误')
        return value

    def test(self):
        started = time.monotonic()
        result = self.complete('This is a connectivity check. Reply with exactly OK.', 'OK')
        return {'ok': result.strip().rstrip('.') == 'OK', 'reply': result[:100], 'seconds': round(time.monotonic() - started, 2), 'model': self.profile['model']}
