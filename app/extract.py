"""Extract explicit Guangya links only; never follow arbitrary message URLs."""
import re
from urllib.parse import urlparse, parse_qs, unquote

URL = re.compile(r'https?://[^\s<>"\u3000]+', re.I)
CODE = re.compile(r'(?:提取码|访问码|密码)\s*[:：]?\s*([A-Za-z0-9]{4,12})')


def resource_title(message):
    """Prefer a labelled title, otherwise the first non-metadata message line."""
    lines = [line.strip() for line in (message or '').splitlines() if line.strip()]
    for line in lines:
        match = re.match(r'^[\W_]*(?:片名|剧名|名称|资源名称|标题)\s*[:：]\s*(.+)', line)
        if match:
            return URL.sub('', match.group(1)).strip()[:240]
    for line in lines:
        clean = re.sub(r'^[^\w\u4e00-\u9fff《]+', '', line).strip()
        if not clean or URL.search(clean) or CODE.search(clean):
            continue
        if re.match(r'^(?:类型|TMDB|上映|评分|来源|画质|编码|音频|大小|简介|标签|点击|链接|分享|资源链接|光鸭云盘)', clean, re.I):
            continue
        return clean[:240]
    return ''


def extract(text, extra_urls=()):
    found = {}
    text_code = CODE.search(text or '')
    for url in [*URL.findall(text or ''), *extra_urls]:
        url = url.rstrip('。，；、！）)]}›>')
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in (
            'guangyapan.com', 'www.guangyapan.com', 'app.guangyapan.com'
        ) or parsed.username or parsed.password:
            continue
        match = re.fullmatch(r'/s/([A-Za-z0-9_-]+)/?', parsed.path)
        if not match:
            continue
        share = match.group(1)
        query = parse_qs(parsed.query)
        code = next((query[k][0] for k in ('pwd', 'password', 'pass_code', 'code') if query.get(k)), '')
        code = code or (text_code.group(1) if text_code else '')
        found[share] = {'share_id': share, 'url': f'https://www.guangyapan.com/s/{share}', 'code': unquote(code)}
    return list(found.values())
