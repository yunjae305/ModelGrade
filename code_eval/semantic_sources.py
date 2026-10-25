"""Read public source text and verify quotes without persisting page contents.

Retrieval proves quotation provenance, not publisher authority or claim truth.
Only URLs already observed by the provider's completed search may be retrieved.
Successful public text may be reused briefly in a bounded process-memory cache.
"""
from __future__ import annotations

import copy
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import sys
import threading
import time
import unicodedata
import zlib
from datetime import datetime, timezone
from collections import OrderedDict
from concurrent.futures import CancelledError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

MAX_SOURCE_URLS = 8
MAX_SOURCE_REFERENCES = 128
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_TIMEOUT = 8.0
TOTAL_TIMEOUT = 30.0
DNS_TIMEOUT = 3.0
SOURCE_CACHE_TTL = 300.0
SOURCE_CACHE_MAX_URLS = 32
SOURCE_CACHE_MAX_BYTES = 8 * 1024 * 1024
_DNS_SLOTS = threading.BoundedSemaphore(4)
_REDIRECTS = {301, 302, 303, 307, 308}
_TEXT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
_CHARSETS = {"utf-8", "utf8", "utf-8-sig", "ascii", "us-ascii", "iso-8859-1",
             "latin-1", "windows-1252", "cp1252", "euc-kr", "cp949", "utf-16", "utf-16le", "utf-16be"}
_QUOTE_TYPOGRAPHY = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
#: Markdown 을 벗긴 형태로 다시 대조하는 인용 길이 상한 — 인용은 짧은 구절이고, 짝 없는 '**' 가 많은 긴 문자열의 정규식 비용을 막는다
_MARKDOWN_QUOTE_LIMIT = 2000
#: 이 글자가 있는 인용은 Markdown 을 벗기지 않는다 — 이스케이프·백틱·문자열 리터럴·대입·딕셔너리 (코드 속 ** 와 구분 불가)
_CODE_MARKS = ("\\", "`", '"', "=", "{", "}")
#: 평가 모델의 web_fetch 는 HTML 을 Markdown 으로 바꿔 주므로 인용에 '**O(log n)**' 처럼 굵게 표시가 붙어 온다 — 앞이 공백·문장 시작이고
#: 뒤가 공백·문장부호인 짝만 굵게 표시로 본다 ('2**3'·'func(**kw)'·'x ** y' 같은 코드 기호는 그대로). 백틱은 셸 명령 치환과
#: 구분할 수 없어 벗기지 않는다.
_MD_STRONG = re.compile(r"(?:(?<=\s)|^)\*\*(?=\S)(.+?)(?<=\S)\*\*(?=[\s.,;:!?)]|$)")


class SourceError(Exception):
    def __init__(self, status, detail=None):
        self.status, self.detail = status, detail
        super().__init__(status)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _same_site(host, other):
    """같은 발행처 — 호스트가 같거나 앞의 'www.' 만 다르다. 다른 하위 도메인·도메인은 다른 발행처다."""
    strip = lambda name: name[4:] if name.startswith("www.") else name
    return host == other or strip(host) == strip(other)


def _url(value):
    """Match provider URL identity; access restrictions are checked separately."""
    if not isinstance(value, str) or len(value) > 8192 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        parts.port
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def _target(value):
    normalized = _url(value)
    if not normalized:
        raise SourceError("blocked_url")
    parts = urlsplit(normalized)
    port = 443 if parts.scheme == "https" else 80
    if parts.port is not None and parts.port != port:
        raise SourceError("blocked_port")
    try:
        hostname = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise SourceError("blocked_url") from None
    if not hostname or len(hostname) > 253 or "%" in hostname or any(c in hostname for c in "\r\n/#?@"):
        raise SourceError("blocked_url")
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parts.query:
        path += "?" + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    return normalized, hostname, port, host_header, path, parts.scheme == "https"


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise SourceError("timeout")
    return seconds


def _public_address(value):
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        elif isinstance(address, ipaddress.IPv6Address):
            # Transition/translation addresses can route to an embedded private IPv4.
            if address.sixtofour or address.teredo or address in ipaddress.ip_network("64:ff9b::/96"):
                return False
        if address == ipaddress.ip_address("168.63.129.16"):
            return False  # Azure's host-local platform service uses a global-looking IP.
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


def _resolve_public(hostname, port, deadline):
    """Bound OS resolution and reject the entire answer if any address is private."""
    dns_deadline = min(deadline, time.monotonic() + DNS_TIMEOUT)
    if not _DNS_SLOTS.acquire(timeout=_remaining(dns_deadline)):
        raise SourceError("timeout" if time.monotonic() >= deadline else "dns_busy")
    results = queue.Queue(maxsize=1)
    def resolve():
        try:
            results.put((True, socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)))
        except Exception:
            results.put((False, None))
        finally:
            _DNS_SLOTS.release()
    worker = threading.Thread(target=resolve, daemon=True)
    try:
        _remaining(dns_deadline)
        worker.start()
    except SourceError:
        _DNS_SLOTS.release()
        raise
    except RuntimeError:
        _DNS_SLOTS.release()
        raise SourceError("dns_busy") from None
    try:
        success, addresses = results.get(timeout=_remaining(dns_deadline))
    except queue.Empty:
        raise SourceError("dns_timeout") from None
    if not success or not addresses or len(addresses) > 64:
        raise SourceError("dns_error")
    pinned = []
    for family, kind, protocol, _, sockaddr in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM or not _public_address(sockaddr[0]):
            raise SourceError("blocked_address")
        # Resolver-provided scope IDs and ports must not alter the intended target.
        if sockaddr[1] != port or family == socket.AF_INET6 and sockaddr[3] != 0:
            raise SourceError("blocked_address")
        if (family, protocol, sockaddr) not in pinned:
            pinned.append((family, protocol, sockaddr))
    return pinned


class _PinnedConnection(http.client.HTTPConnection):
    """Never resolve the Host header while connecting; TLS still verifies that host."""
    def __init__(self, hostname, port, address, secure, deadline):
        super().__init__(hostname, port, timeout=min(5.0, _remaining(deadline)))
        self.address, self.secure, self.deadline = address, secure, deadline
        self._transport_socket = None
        self.aborted = False   # 마감 타이머가 끊었는가 — 끊긴 뒤의 읽기 오류는 모두 '시간 초과'

    def connect(self):
        family, protocol, sockaddr = self.address
        self.sock = socket.socket(family, socket.SOCK_STREAM, protocol)
        self._transport_socket = self.sock
        try:
            self.sock.settimeout(min(5.0, _remaining(self.deadline)))
            self.sock.connect(sockaddr)
            if self.secure:
                context = ssl.create_default_context()
                self.sock = context.wrap_socket(self.sock, server_hostname=self.host, do_handshake_on_connect=False)
                self._transport_socket = self.sock
                self.sock.settimeout(min(5.0, _remaining(self.deadline)))
                self.sock.do_handshake()
        except Exception:
            self.close()
            raise

    def abort(self):
        # getresponse() clears self.sock for Connection: close while the response's
        # makefile still holds the underlying descriptor. Shutdown must reach that
        # descriptor to interrupt a slow body read, not only an attached connection.
        self.aborted = True
        sock = self._transport_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class _VisibleHTML(HTMLParser):
    _HIDDEN = {"script", "style", "template", "noscript", "head"}
    _BLOCKS = {"p", "div", "br", "hr", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "blockquote", "pre", "ul", "ol", "table"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden, self.parts = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        elif not self.hidden and tag in self._BLOCKS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.hidden:
            self.hidden = self.hidden[:len(self.hidden) - 1 - self.hidden[::-1].index(tag)]
        elif not self.hidden and tag in self._BLOCKS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _normalize(text):
    # Normalize typographic quotes only; retain punctuation and mathematical
    # primes so a different quantity or statement cannot become a matching quote.
    return " ".join(unicodedata.normalize("NFKC", html.unescape(text)).translate(_QUOTE_TYPOGRAPHY).split()) if isinstance(text, str) else ""


def _quote_in(text, quote):
    """정규화한 원문(text)에 정규화한 인용(quote)이 있는가. 인용 그대로 없으면, 인용에서 Markdown 굵게 표시(_MD_STRONG)만
    벗긴 형태로 한 번 더 본다 — 원문 쪽은 바꾸지 않으므로 원문에 실제로 그 글자들이 있어야 맞는다."""
    if quote in text:
        return True
    # 코드가 섞였을 수 있는 인용은 글자 그대로만 본다 — 긴 인용, 이스케이프(\\), 백틱(인라인 코드·셸 명령 치환),
    # 코드 기호(" = { }: 문자열 리터럴·대입·딕셔너리 안의 ** 는 굵게 표시와 구분할 수 없다)
    if len(quote) > _MARKDOWN_QUOTE_LIMIT or any(mark in quote for mark in _CODE_MARKS):
        return False
    plain = _MD_STRONG.sub(r"\1", quote)
    return plain != quote and plain in text


def _decode_text(data, content_type, charset):
    if not charset and content_type != "text/plain":
        found = re.search(br"<meta\b[^>]{0,1024}?charset\s*=\s*[\"']?\s*([A-Za-z0-9_-]+)", data[:8192], re.I)
        charset = found.group(1).decode("ascii") if found else None
    charset = (charset or "utf-8-sig").strip().lower()
    if charset not in _CHARSETS:
        raise SourceError("unsupported_encoding")
    try:
        text = data.decode(charset)
    except (UnicodeError, LookupError):
        raise SourceError("decode_error") from None
    if content_type == "text/plain":
        return text
    parser = _VisibleHTML()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def _read_body(response, encoding, deadline):
    """Bound both wire and decoded bytes; accept only one complete compressed stream."""
    compressed = encoding in ("gzip", "deflate")
    wire_limit = MAX_COMPRESSED_BYTES if compressed else MAX_RESPONSE_BYTES
    length = response.getheader("Content-Length")
    if length and (not length.isdecimal() or int(length) > wire_limit):
        raise SourceError("response_too_large")
    decoder = zlib.decompressobj(zlib.MAX_WBITS + (16 if encoding == "gzip" else 0)) if compressed else None
    chunks, wire_size, decoded_size = [], 0, 0
    while True:
        _remaining(deadline)
        chunk = response.read(min(65536, wire_limit + 1 - wire_size))
        _remaining(deadline)
        if not chunk:
            break
        wire_size += len(chunk)
        if wire_size > wire_limit:
            raise SourceError("response_too_large")
        if decoder is not None:
            try:
                # The extra byte detects a bomb without allocating its full output.
                chunk = decoder.decompress(chunk, MAX_RESPONSE_BYTES + 1 - decoded_size)
            except zlib.error:
                raise SourceError("invalid_compressed_body") from None
            if decoder.unused_data:
                # Concatenated gzip members and trailing junk are intentionally unsupported.
                raise SourceError("trailing_compressed_data")
        decoded_size += len(chunk)
        if decoded_size > MAX_RESPONSE_BYTES:
            raise SourceError("response_too_large")
        _remaining(deadline)
        chunks.append(chunk)
    _remaining(deadline)
    if length and wire_size != int(length):
        raise SourceError("incomplete_body")
    if decoder is not None and not decoder.eof:
        raise SourceError("incomplete_compressed_body")
    # No flush(): max_length has already bounded all output and eof proves that
    # the complete stream, including its checksum/trailer, was processed.
    _remaining(deadline)
    return b"".join(chunks)


def _request(target, address, deadline):
    _, hostname, port, host_header, path, secure = target
    connection = _PinnedConnection(hostname, port, address, secure, deadline)
    timer = threading.Timer(_remaining(deadline), connection.abort)
    timer.daemon = True
    timer.start()
    response = None
    try:
        # stdlib HTTP connections do not inherit proxy, cookie, or auth settings.
        connection.request("GET", path, headers={"Host": host_header, "Accept": "text/html,application/xhtml+xml,text/plain",
                           "Accept-Encoding": "identity", "User-Agent": "AIONA-Evidence/1.0", "Connection": "close"})
        response = connection.getresponse()
        _remaining(deadline)
        if response.status in _REDIRECTS:
            return {"redirect": response.getheader("Location")}
        if response.status != 200:
            raise SourceError("http_error", f"http_{response.status}")
        content_type = response.headers.get_content_type().lower()
        if content_type not in _TEXT_TYPES:
            raise SourceError("unsupported_content_type")
        encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
        if encoding not in ("identity", "gzip", "deflate"):
            raise SourceError("unsupported_content_encoding")
        data = _read_body(response, encoding, deadline)
        text = _decode_text(data, content_type, response.headers.get_content_charset())
        _remaining(deadline)
        return {"text": text, "content_type": content_type}
    except (SourceError, OSError, http.client.HTTPException) as exc:
        # 마감 타이머가 연결을 끊은 뒤의 오류(본문 불완전 · 소켓 오류)는 원인이 시간 초과다 — 타이머는 OS 해상도 때문에
        # 마감보다 조금 먼저 울릴 수 있어 _remaining 만으로는 못 잡는다 (10-25 밤: Windows CI 에서 'incomplete_body')
        if getattr(connection, "aborted", False) and not (isinstance(exc, SourceError) and exc.status == "timeout"):
            raise SourceError("timeout") from None
        raise
    finally:
        timer.cancel()
        try:
            if response is not None:
                response.close()
        finally:
            connection.close()


def fetch_public_source(url, *, deadline=None):
    """Fetch bounded public HTML/plain text. Failures return no partial page text."""
    deadline = min(deadline, time.monotonic() + FETCH_TIMEOUT) if deadline is not None else time.monotonic() + FETCH_TIMEOUT
    current = url
    original_hostname = None
    try:
        for hop in range(MAX_REDIRECTS + 1):
            target = _target(current)
            if original_hostname is None:
                original_hostname = target[1]
            elif not _same_site(target[1], original_hostname):
                # The model assessed the cited publisher, not a different host.
                # Only a leading 'www.' may differ; every hop is still DNS/IP/TLS checked.
                raise SourceError("redirect_host_changed")
            addresses = _resolve_public(target[1], target[2], deadline)
            # Pin a validated address. A retry never changes hosts or bypasses DNS checks.
            result = _request(target, addresses[0], deadline)
            if "redirect" not in result:
                return {"status": "ok", **result, "final_url": target[0], "checked_at": _now()}
            if hop == MAX_REDIRECTS:
                raise SourceError("redirect_limit")
            location = result["redirect"]
            if not isinstance(location, str) or not location.strip():
                raise SourceError("invalid_redirect")
            current = urljoin(target[0], location)
        raise SourceError("redirect_limit")
    except SourceError as exc:
        status, detail = exc.status, exc.detail
    except (TimeoutError, socket.timeout):
        status, detail = "timeout", None
    except ssl.SSLCertVerificationError:
        # Certificate verification stays on; record why the page could not be read.
        status, detail = "fetch_error", "tls_certificate"
    except Exception:
        # Do not persist network exception strings, local addresses, or page content.
        status, detail = "fetch_error", None
    failure = {"status": status, "text": "", "final_url": None, "checked_at": _now(), "content_type": None}
    if detail:
        failure["detail"] = detail
    return failure


class _SourceFlight:
    def __init__(self):
        self.ready = threading.Event()
        self.result = None


class _SourceTextCache:
    """Share only successful public fetches, never search authorization or verdicts."""
    def __init__(self, *, ttl=SOURCE_CACHE_TTL, max_urls=SOURCE_CACHE_MAX_URLS,
                 max_bytes=SOURCE_CACHE_MAX_BYTES, clock=None):
        self.ttl, self.max_urls, self.max_bytes = ttl, max_urls, max_bytes
        self._clock = clock or time.monotonic
        self._entries, self._flights = OrderedDict(), {}
        self._bytes = 0
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)

    @staticmethod
    def _failure(status):
        return {"status": status, "text": "", "final_url": None,
                "checked_at": _now(), "content_type": None}

    def _prune(self, now):
        for key, (expires, _, size) in list(self._entries.items()):
            if expires <= now:
                self._entries.pop(key)
                self._bytes -= size

    def _store(self, url, hostname, item):
        if item.get("status") != "ok" or not isinstance(item.get("text"), str):
            return
        if item.get("content_type") not in _TEXT_TYPES or not isinstance(item.get("checked_at"), str):
            return
        try:
            if not _same_site(_target(item.get("final_url"))[1], hostname):
                return
        except SourceError:
            return
        # Retain only the fetcher's text and provenance, never a model response.
        value = {key: item[key] for key in ("status", "text", "final_url", "checked_at", "content_type")}
        size = sys.getsizeof(url) + sys.getsizeof(value) + sum(
            sys.getsizeof(key) + sys.getsizeof(field) for key, field in value.items())
        if size > self.max_bytes or self.max_urls <= 0 or self.ttl <= 0:
            return
        previous = self._entries.pop(url, None)
        if previous is not None:
            self._bytes -= previous[2]
        while self._entries and (len(self._entries) >= self.max_urls or self._bytes + size > self.max_bytes):
            _, (_, _, removed) = self._entries.popitem(last=False)
            self._bytes -= removed
        self._entries[url] = (self._clock() + self.ttl, value, size)
        self._bytes += size

    def fetch(self, url, *, deadline):
        # No DNS/network access occurs on a cache hit. Every new download still
        # uses fetch_public_source's public-DNS, pinned-IP, TLS and redirect checks.
        deadline = min(deadline, time.monotonic() + FETCH_TIMEOUT)
        try:
            _remaining(deadline)
            target = _target(url)
        except SourceError as exc:
            return self._failure(exc.status)
        url, hostname = target[:2]
        with self._changed:
            while True:
                try:
                    _remaining(deadline)
                    self._prune(self._clock())
                    entry = self._entries.get(url)
                    if entry is not None:
                        self._entries.move_to_end(url)
                        return dict(entry[1])
                    flight = self._flights.get(url)
                    leader = flight is None
                    if not leader:
                        break
                    if len(self._flights) < max(1, self.max_urls):
                        flight = self._flights[url] = _SourceFlight()
                        break
                    # Bounded in-flight bookkeeping. Waiting for a free slot
                    # spends this caller's existing deadline, without a retry.
                    self._changed.wait(_remaining(deadline))
                except SourceError as exc:
                    return self._failure(exc.status)
        if not leader:
            try:
                if not flight.ready.wait(_remaining(deadline)):
                    return self._failure("timeout")
                _remaining(deadline)
                return dict(flight.result)
            except SourceError as exc:
                return self._failure(exc.status)
        item = self._failure("fetch_error")
        try:
            fetched = fetch_public_source(url, deadline=deadline)
            _remaining(deadline)
            if isinstance(fetched, dict):
                item = fetched
        except SourceError as exc:
            item = self._failure(exc.status)
        except Exception:
            pass
        finally:
            with self._lock:
                try:
                    self._prune(self._clock())
                    self._store(url, hostname, item)
                finally:
                    # Existing waiters share this attempt's failure too;
                    # subsequent calls retry because failures aren't cached.
                    flight.result = dict(item)
                    self._flights.pop(url, None)
                    flight.ready.set()
                    self._changed.notify_all()
        return dict(item)


_SOURCE_TEXT_CACHE = _SourceTextCache()


_REASONS = {
    "verified": "검색한 출처의 본문에서 인용문을 확인했습니다.",
    "quote_not_found": "검색한 출처의 본문에서 인용문을 확인하지 못했습니다.",
    "empty_quote": "대조할 인용문이 없습니다.",
    "not_observed": "완료된 검색 결과나 원문 열기 기록에서 이 출처 URL을 확인하지 못했습니다.",
    "source_limit": "이번 평가의 출처 수집 한도를 초과했습니다.",
    "timeout": "출처 원문 확인 제한 시간을 초과했습니다.",
    "redirect_host_changed": "출처가 다른 호스트로 이동해 기존 공신력 판단을 적용할 수 없습니다.",
    "unsupported_content_encoding": "지원하지 않는 압축 형식이어서 출처 본문을 복원하지 못했습니다.",
    "unsupported_content_type": "HTML·일반 텍스트 이외의 자료여서 출처 원문을 대조하지 못했습니다.",
    "http_error": "출처 서버가 정상 응답을 보내지 않아 본문을 읽지 못했습니다.",
    "response_too_large": "출처의 전송 크기 또는 압축 해제 크기가 수집 한도(2 MiB)를 초과했습니다.",
    "decode_error": "출처 본문의 문자 인코딩을 해석하지 못했습니다.",
    "unsupported_encoding": "지원하지 않는 문자 인코딩이어서 출처 본문을 읽지 못했습니다.",
    "fetch_error": "출처 서버 연결 또는 본문 수신에 실패했습니다.",
    "invalid_compressed_body": "압축 본문이 손상되었거나 형식이 올바르지 않아 복원하지 못했습니다.",
    "incomplete_compressed_body": "압축 본문이 완전하지 않아 인용문을 확인하지 못했습니다.",
    "trailing_compressed_data": "압축 본문에 추가 스트림이나 불명확한 데이터가 있어 인용문을 확정하지 않았습니다.",
    "incomplete_body": "출처 본문이 완전하게 수신되지 않아 인용문을 확인하지 못했습니다.",
    "blocked_url": "허용되는 공개 웹 주소 형식이 아니어서 출처를 열지 않았습니다.",
    "blocked_port": "표준 HTTP·HTTPS 접속 포트가 아니어서 출처를 열지 않았습니다.",
    "blocked_address": "공개 인터넷 주소로 확인되지 않아 출처를 열지 않았습니다.",
    "dns_error": "출처의 인터넷 주소를 확인하지 못했습니다.",
    "dns_timeout": "출처의 인터넷 주소 확인 시간이 초과되었습니다.",
    "dns_busy": "주소 확인 작업이 한도에 도달해 이번 출처를 확인하지 못했습니다.",
    "redirect_limit": "출처 페이지의 연속 이동 횟수가 한도를 초과했습니다.",
    "invalid_redirect": "출처 서버의 페이지 이동 주소가 올바르지 않습니다.",
}


#: 서버 수집이 이 상태로 실패했을 때만 공급자 web_fetch 원문으로 인용문을 대조한다 (F5b).
#: 출처 쪽 접근 거부·연결·시간·크기·형식 문제다. 공개 주소·포트·URL 차단과 다른 호스트·연속 이동·잘못된 이동
#: 같은 보안·발행처 확인 실패에는 쓰지 않는다 — 그 검사는 서버 수집에서 그대로 유지된다.
PROVIDER_DOCUMENT_FALLBACK_STATUSES = frozenset({
    "http_error", "fetch_error", "timeout", "dns_error", "dns_timeout", "dns_busy", "source_limit",
    "response_too_large", "unsupported_content_type", "unsupported_content_encoding", "decode_error",
    "unsupported_encoding", "invalid_compressed_body", "incomplete_compressed_body", "trailing_compressed_data",
    "incomplete_body",
})
_PROVIDER_REASONS = {
    "verified": "서버가 출처 원문을 받지 못해, 평가자가 같은 요청에서 web_fetch 로 받은 원문에서 인용문을 확인했습니다.",
    "quote_not_found": "서버가 출처 원문을 받지 못했고, 평가자가 web_fetch 로 받은 원문에서도 인용문을 확인하지 못했습니다.",
}


def _provider_document(documents, url, status):
    """서버 수집 실패를 공급자 원문으로 대신할 수 있으면 그 원문 목록. 원문은 메모리에서만 쓴다."""
    if status not in PROVIDER_DOCUMENT_FALLBACK_STATUSES or not isinstance(documents, dict):
        return None
    found = [doc for doc in documents.get(url) or [] if isinstance(doc, dict) and isinstance(doc.get("text"), str)]
    return found or None


def _detail(item):
    """접근 실패 원인 진단값(HTTP 상태·인증서) — 수집기가 정한 형식만 기록한다. 판정 정책에는 쓰지 않는다."""
    value = item.get("detail") if isinstance(item, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"http_\d{3}|tls_certificate", value) else None


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Source verification cancelled")


def verify_web_sources(body, *, fetcher=None, cancel_event=None, provider_documents=None):
    """Verify retrieved quotes, or raise CancelledError without a partial result.

    An in-flight shared download retains its bounded deadline for other callers;
    cancellation is checked when it returns and before opening another source.
    ``provider_documents`` (URL → texts the provider's own web_fetch returned in this
    request, memory only) stand in only when the server's fetch failed for an access
    reason; the quote must still be found in that text.
    """
    _check_cancelled(cancel_event)
    result = copy.deepcopy(body)
    answers = result.get("answers") if isinstance(result, dict) else None
    j5 = answers.get("J5") if isinstance(answers, dict) else None
    fact = j5.get("fact_check") if isinstance(j5, dict) else None
    if not isinstance(fact, dict) or fact.get("basis") not in ("web", "mixed"):
        _check_cancelled(cancel_event)
        return result
    verification = result.get("verification")
    if not isinstance(verification, dict):
        verification = result["verification"] = {}
    sources = []
    for claim in fact.get("claims", []) if isinstance(fact.get("claims"), list) else []:
        _check_cancelled(cancel_event)
        if not isinstance(claim, dict):
            continue
        for source in claim.get("sources", []) if isinstance(claim.get("sources"), list) else []:
            _check_cancelled(cancel_event)
            if not isinstance(source, dict):
                continue
            # Input/reference quotes are checked against their exact original by
            # the finalizer. They must not consume a public-web retrieval slot.
            origin = source.get("origin") or ("web" if fact.get("basis") == "web" else None)
            if origin != "web":
                continue
            sources.append(source)
    _verify_sources(sources, verification, fetcher=fetcher, cancel_event=cancel_event, provider_documents=provider_documents)
    _check_cancelled(cancel_event)
    return result


_CLAIM_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
#: 주장 문장에 붙은 출처 표기. 수치는 이것을 걷어낸 본문에서만 읽는다 (URL 경로·도메인·쿼리·인용 번호의 숫자는 주장 수치가 아님).
_LINK_TARGET = r"\[[^\]\n]{1,200}\]\([^)\s]+(?:\s+\"[^\"\n]*\")?\)"
_CITATION_GROUP = re.compile(r"[(（]\s*(?:(?:출처|참고(?:\s*자료)?|자료|source|ref(?:erence)?)\s*[:：]?\s*)?"
                             r"(?:" + _LINK_TARGET + r"\s*[,;·]?\s*)+[)）]", re.I)
_MD_LINK = re.compile(r"(?<!!)\[(?P<text>[^\]\n]{1,200})\]\([^)\s]+(?:\s+\"[^\"\n]*\")?\)")
_URL_TEXT = re.compile(r"<?(?:https?://|www\.)[^\s<>()\[\]\"'`]+>?", re.I)
_CITATION_MARK = re.compile(r"\[\^[^\]\s]{1,64}\]|\[\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*\]")


def _claim_numbers(claim):
    """출처 표기를 뺀 주장 본문의 수치. 괄호 안 출처 링크는 통째로, 본문 속 링크는 주소만 빼고 표시 글자는 남긴다."""
    text = _CITATION_GROUP.sub(" ", claim or "")
    text = _MD_LINK.sub(lambda m: m.group("text"), text)
    text = _URL_TEXT.sub(" ", text)
    return _CLAIM_NUMBER.findall(_CITATION_MARK.sub(" ", text))


def verify_answer_citations(body, state, *, fetcher=None, cancel_event=None, provider_documents=None):
    """답변이 직접 든 출처 URL 을 서버가 직접 받아 평가자의 출처 판정을 대조한다 (원문은 저장하지 않음).

    평가자가 '열지 못함'이라 했어도 서버가 받으면 그 판정을 쓰지 않고, 서버가 받지 못하면 어떤 판정도
    확정하지 않는다. 기록: verification.citation_sources (URL 별 수집 상태)와 각 citation_checks 항목의
    server_status · quote_verified · claim_numbers_in_source · retrieval. 판정 반영은 finalize 단계가 한다.
    서버가 접근 문제로 받지 못한 URL 은 같은 요청에서 공급자 web_fetch 가 받은 원문(``provider_documents``)으로
    대조한다 (retrieval=provider_document). 보안·발행처 확인 실패는 대신하지 않는다.
    """
    _check_cancelled(cancel_event)
    result = copy.deepcopy(body)
    citations = state.get("answer_citations") if isinstance(state, dict) else None
    answers = result.get("answers") if isinstance(result, dict) else None
    j5 = answers.get("J5") if isinstance(answers, dict) else None
    if not isinstance(citations, list) or not citations or not isinstance(j5, dict):
        return result
    verification = result.get("verification")
    if not isinstance(verification, dict):
        verification = result["verification"] = {}
    deadline = time.monotonic() + TOTAL_TIMEOUT
    pages, sources = {}, []
    for item in citations[:MAX_SOURCE_URLS]:
        _check_cancelled(cancel_event)
        url = _url(item.get("url")) if isinstance(item, dict) else None
        if not url or url in pages:
            continue
        if time.monotonic() >= deadline:
            page = {"status": "timeout"}
        else:
            try:
                page = fetcher(url) if fetcher is not None else _SOURCE_TEXT_CACHE.fetch(url, deadline=deadline)
                _check_cancelled(cancel_event)
                if (isinstance(page, dict) and page.get("status") == "ok" and page.get("final_url")
                        and not _same_site(_target(page["final_url"])[1], _target(url)[1])):
                    page = {**page, "status": "redirect_host_changed", "text": ""}
            except CancelledError:
                raise
            except Exception:
                _check_cancelled(cancel_event)
                page = {"status": "fetch_error"}
        page = page if isinstance(page, dict) else {"status": "fetch_error"}
        pages[url] = page
        sources.append({"url": url, "status": page.get("status", "fetch_error"),
                        "resolved_url": _url(page.get("final_url")), "checked_at": page.get("checked_at") or _now(),
                        "reason": "출처 원문을 받았습니다." if page.get("status") == "ok" else
                        _REASONS.get(page.get("status"), "출처 원문을 안전하게 수집할 수 없었습니다.")})
        if _detail(page):
            sources[-1]["detail"] = _detail(page)
        if page.get("status") != "ok" and _provider_document(provider_documents, url, page.get("status")):
            sources[-1]["provider_document"] = True
    verification["citation_sources"] = sources
    checks = j5.get("citation_checks")
    for check in checks if isinstance(checks, list) else []:
        _check_cancelled(cancel_event)
        if not isinstance(check, dict):
            continue
        url = _url(check.get("url"))
        page = pages.get(url)
        check.update(server_status=None if page is None else page.get("status", "fetch_error"),
                     quote_verified=False, claim_numbers_in_source=None, retrieval=None)
        if page is None:
            continue
        if page.get("status") == "ok":
            texts, check["retrieval"] = [_normalize(page.get("text"))], "server"
        else:
            documents = _provider_document(provider_documents, url, page.get("status"))
            if not documents:
                continue
            texts, check["retrieval"] = [_normalize(d["text"]) for d in documents], "provider_document"
        quote = _normalize(check.get("quote"))
        check["quote_verified"] = bool(quote) and any(_quote_in(text, quote) for text in texts)
        numbers = _claim_numbers(check.get("claim"))
        if numbers:
            check["claim_numbers_in_source"] = any(all(_normalize(n) in text for n in numbers) for text in texts)
    _check_cancelled(cancel_event)
    return result


def _verify_sources(sources, verification, *, fetcher=None, cancel_event=None, provider_documents=None):
    """Verify a flat list of web sources in place (quote_verified/verification_status/…), recording source_checks.

    Shared by the legacy fact_check path (``verify_web_sources``) and the items pipeline
    (``semantic_facts.verify_refutations``): only URLs observed by the provider's completed
    search or opened by its web_fetch are opened, at most MAX_SOURCE_URLS per call, within TOTAL_TIMEOUT.
    ``provider_documents`` stand in only when the server's fetch failed for an access reason.
    """
    observed = {_url(value) for value in verification.get("source_urls", []) if isinstance(value, str)} if isinstance(verification.get("source_urls"), list) else set()
    observed.discard(None)
    # A page the provider opened directly (web_fetch) is observed retrieval even
    # without a search. Only the transport's provider blocks populate this list.
    opened = {_url(value) for value in verification.get("fetched_urls", []) if isinstance(value, str)} if isinstance(verification.get("fetched_urls"), list) else set()
    opened.discard(None)
    search_performed = verification.get("search_performed") is True
    verification.update(source_validation="provider_and_content", source_checks=[])
    deadline = time.monotonic() + TOTAL_TIMEOUT
    cache, checks, fetched = {}, {}, 0
    references = 0
    for source in sources:
        _check_cancelled(cancel_event)
        if not isinstance(source, dict):
            continue
        references += 1
        source.update(quote_verified=False, verification_reason="", verified_at=None, resolved_url=None, retrieval=None)
        source.pop("server_fetch_status", None)   # server-owned audit field, never taken from model output
        documents = None
        url = _url(source.get("url"))
        quote_text = _normalize(source.get("quote"))
        if not url or url not in observed or not (search_performed or url in opened):
            status = "not_observed"
        elif references > MAX_SOURCE_REFERENCES:
            status = "source_limit"
        elif not quote_text:
            status = "empty_quote"
        else:
            if url not in cache:
                if fetched >= MAX_SOURCE_URLS:
                    cache[url] = {"status": "source_limit", "checked_at": _now()}
                elif time.monotonic() >= deadline:
                    cache[url] = {"status": "timeout", "checked_at": _now()}
                else:
                    fetched += 1
                    try:
                        # Injected fetchers bypass shared state entirely, so
                        # independent test/client fixtures cannot contaminate it.
                        _check_cancelled(cancel_event)
                        item = fetcher(url) if fetcher is not None else _SOURCE_TEXT_CACHE.fetch(url, deadline=deadline)
                        _check_cancelled(cancel_event)
                        if isinstance(item, dict) and item.get("status") == "ok":
                            if not _same_site(_target(item.get("final_url"))[1], _target(url)[1]):
                                item = {**item, "status": "redirect_host_changed", "text": ""}
                        cache[url] = item if isinstance(item, dict) else {"status": "fetch_error"}
                    except CancelledError:
                        raise
                    except Exception:
                        _check_cancelled(cancel_event)
                        cache[url] = {"status": "fetch_error"}
                item = cache[url]
                checks[url] = {"url": url, "status": item.get("status", "fetch_error"),
                               "checked_at": item.get("checked_at") or _now(), "resolved_url": _url(item.get("final_url")),
                               "quote_count": 0, "verified_quotes": 0, "unverified_quotes": 0}
                if _detail(item):
                    checks[url]["detail"] = _detail(item)
                if _provider_document(provider_documents, url, checks[url]["status"]) and url in opened:
                    checks[url]["provider_document"] = True
                verification["source_checks"].append(checks[url])
            item = cache[url]
            status = item.get("status", "fetch_error")
            documents = _provider_document(provider_documents, url, status) if url in opened else None
            if status == "ok":
                status = "verified" if _quote_in(_normalize(item.get("text")), quote_text) else "quote_not_found"
            elif documents:
                # The server could not read the page; the provider's own fetch of the same URL could.
                source["server_fetch_status"] = status
                status = "verified" if any(_quote_in(_normalize(d["text"]), quote_text) for d in documents) else "quote_not_found"
            checks[url]["quote_count"] += 1
            checks[url]["verified_quotes" if status == "verified" else "unverified_quotes"] += 1
            if status == "verified" and documents:
                source.update(quote_verified=True, verified_at=documents[0].get("retrieved_at") or checks[url]["checked_at"],
                              resolved_url=url)
            elif status == "verified":
                source.update(quote_verified=True, verified_at=checks[url]["checked_at"], resolved_url=checks[url]["resolved_url"])
            elif checks[url]["resolved_url"]:
                source["resolved_url"] = checks[url]["resolved_url"]
        if status != "not_observed":
            # How the provider reached this URL: opened directly, or only listed in search results.
            # provider_document: the server's fetch failed and the provider's web_fetch text was compared.
            source["retrieval"] = ("provider_document" if documents else
                                   "web_fetch" if url in opened else "web_search")
        source["verification_status"] = status
        source["verification_reason"] = (_PROVIDER_REASONS[status] if documents else
                                         _REASONS.get(status, "출처 원문을 안전하게 수집할 수 없어 인용문을 확인하지 못했습니다."))
    _check_cancelled(cancel_event)
    return sources
