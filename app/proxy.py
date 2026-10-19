"""HTTP proxy configuration. Errors never include the supplied URL or credentials."""
import ipaddress
import os
import re
from urllib.parse import urlsplit, unquote

import socks


class ProxyConfigError(ValueError):
    pass


def http_proxy(value, variable='TELEGRAM_PROXY_URL'):
    if value is None or value == '':
        return None

    def fail(reason):
        raise ProxyConfigError(f'{variable} 配置错误：{reason}') from None

    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        fail('URL 不能包含空白或控制字符')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        fail('主机或端口格式无效')
    if parsed.scheme != 'http':
        fail('仅支持 http:// HTTP CONNECT 代理，不支持 HTTPS、SOCKS 或 MTProxy 协议')
    if not host:
        fail('必须提供代理主机')
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment or '?' in value or '#' in value:
        fail('代理 URL 不能包含路径、查询参数或片段')
    if parsed.netloc.endswith(':') or (port is not None and not 1 <= port <= 65535):
        fail('端口必须为 1–65535 的整数')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if ':' in host or re.fullmatch(r'[0-9.]+', host):
            fail('IP 地址无效')
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            fail('主机名无效')
        if len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                                      for label in host.rstrip('.').split('.')):
            fail('主机名无效')
    username = password = None
    if '@' in parsed.netloc:
        if parsed.netloc.count('@') != 1 or not parsed.username or not parsed.password:
            fail('认证信息必须包含非空用户名和密码，特殊字符请使用百分号编码')
        encoded = (parsed.username, parsed.password)
        if any(re.search(r'%(?![0-9a-fA-F]{2})', item) for item in encoded):
            fail('认证信息的百分号编码无效')
        try:
            username, password = (unquote(item, errors='strict') for item in encoded)
        except UnicodeError:
            fail('认证信息必须是有效 UTF-8 编码')
        if ':' in username or any(ord(c) < 32 or ord(c) == 127 for c in username + password):
            fail('认证信息包含不允许的字符')
    return (socks.HTTP, host, port or 8080, True, username, password)


def validate_http_environment():
    # HTTPX also accepts lowercase names and ALL_PROXY. Validate all configured
    # values rather than letting its parser echo an invalid credential-bearing URL.
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        http_proxy(os.environ.get(name), name)
