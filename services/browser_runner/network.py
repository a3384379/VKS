from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from threading import Timer
from urllib.parse import urlsplit, urlunsplit


class NetworkDenied(ValueError):
    pass


DENIED_PORTS = {22, 25, 53, 111, 135, 139, 445, 2375, 2376, 3306, 5432, 5672, 6379, 8000, 5173, 8788, 9200, 11211}
DENIED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in ("169.254.0.0/16", "100.100.100.200/32", "0.0.0.0/8", "224.0.0.0/4", "::/128", "fe80::/10", "ff00::/8")
]


def origin(url):
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.fragment:
        raise NetworkDenied("仅允许不含凭据的 HTTP(S) 地址")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise NetworkDenied("地址端口无效") from None
    host = p.hostname.lower().rstrip(".")
    host = f"[{host}]" if ":" in host else host
    return f"{p.scheme}://{host}:{port}"


class NetworkPolicy:
    def __init__(self, base_origin, allowed_origins, allowed_cidrs, *, allow_loopback=False, write_paths=()):
        self.base_origin = origin(base_origin)
        self.origins = {origin(o) for o in allowed_origins}
        if self.base_origin not in self.origins or not 1 <= len(self.origins) <= 20:
            raise NetworkDenied("环境来源必须位于明确的允许清单")
        self.networks = [ipaddress.ip_network(c, strict=True) for c in allowed_cidrs]
        if not self.networks or len(self.networks) > 20:
            raise NetworkDenied("必须设置受控出站网段")
        self.allow_loopback = allow_loopback
        self.write_paths = set(write_paths)

    def resolve(self, url):
        if origin(url) not in self.origins:
            raise NetworkDenied("来源未获授权")
        p = urlsplit(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        if port in DENIED_PORTS:
            raise NetworkDenied("控制服务端口不可用于业务采集")
        try:
            addresses = sorted({a[4][0] for a in socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)})
        except OSError:
            raise NetworkDenied("无法解析登记环境") from None
        if not addresses:
            raise NetworkDenied("登记环境没有有效地址")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if any(ip in n for n in DENIED_NETWORKS if n.version == ip.version) or ip.is_unspecified or ip.is_multicast:
                raise NetworkDenied("元数据或保留地址不可访问")
            if ip.is_loopback and not self.allow_loopback:
                raise NetworkDenied("本地地址需要实例配置显式启用合成测试模式")
            if not any(ip in n for n in self.networks if n.version == ip.version):
                raise NetworkDenied("解析结果越出登记网段")
        return p, addresses[0], port

    def fetch(
        self,
        url,
        method="GET",
        body=None,
        *,
        timeout=10,
        allow_write=False,
        max_bytes=4 * 1024 * 1024,
        headers=None,
        trusted_headers=None,
    ):
        if body is not None and len(body) > 1024 * 1024:
            raise NetworkDenied("请求正文超过合成采集预算")
        p, ip, port = self.resolve(url)
        if method not in ("GET", "HEAD", "OPTIONS") and (not allow_write or p.path not in self.write_paths):
            raise NetworkDenied("写请求未获精确路径授权")
        if method not in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
            raise NetworkDenied("请求方法未获授权")
        # Connect to the validated address directly, closing the DNS-rebinding gap.
        connection = http.client.HTTPConnection(ip, port, timeout=timeout)
        host = (f"[{p.hostname}]" if ":" in p.hostname else p.hostname) + (f":{port}" if port not in (80, 443) else "")
        outgoing = {"Host": host, "Accept-Encoding": "identity", "User-Agent": "VKS-Controlled-Runner/1.0"}
        for k, v in (headers or {}).items():
            if k.lower() in ("content-type", "accept", "accept-language"):
                outgoing[k] = v
        for name, value in (headers or {}).items():
            if (
                name.lower() != "origin"
                or not isinstance(value, str)
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                continue
            try:
                parsed_origin = urlsplit(value)
                if not parsed_origin.path and not parsed_origin.query and origin(value) in self.origins:
                    # Forward the actual browser Origin unchanged. Never synthesize
                    # an Origin for a request that did not provide an allowed one.
                    outgoing[name] = value
            except (ValueError, NetworkDenied):
                pass
        for name, value in (trusted_headers or {}).items():
            if name.lower() not in ("authorization", "x-api-key", "cookie") or any(c in value for c in "\r\n\0"):
                raise NetworkDenied("实例认证头格式无效")
            outgoing[name] = value
        path = urlunsplit(("", "", p.path or "/", p.query, ""))
        deadline = time.monotonic() + timeout

        def close_at_deadline():
            if connection.sock:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        watchdog = Timer(timeout, close_at_deadline)
        watchdog.daemon = True
        watchdog.start()
        try:
            if p.scheme == "https":
                connection.connect()
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=p.hostname)
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            chunks, received = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise NetworkDenied("响应超过采集时间预算")
                chunk = response.read1(min(65536, max_bytes + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received > max_bytes:
                    raise NetworkDenied("响应超过采集体积限制")
            if time.monotonic() >= deadline:
                raise NetworkDenied("响应超过采集时间预算")
            data = b"".join(chunks)
            safe_headers = {
                k: v for k, v in response.getheaders() if k.lower() in ("content-type", "location", "cache-control")
            }
            location = next((v for k, v in safe_headers.items() if k.lower() == "location"), None)
            if location:
                from urllib.parse import urljoin

                self.resolve(urljoin(url, location))
            return response.status, safe_headers, data
        finally:
            watchdog.cancel()
            connection.close()
