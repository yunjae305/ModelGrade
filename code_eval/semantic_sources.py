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


class SourceError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(status)


def _now():
    return datetime.now(timezone.utc).isoformat()


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
            raise SourceError("http_error")
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
            elif target[1] != original_hostname:
                # The model assessed the cited publisher, not a different host.
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
        status = exc.status
    except (TimeoutError, socket.timeout):
        status = "timeout"
    except Exception:
        # Do not persist network exception strings, local addresses, or page content.
        status = "fetch_error"
    return {"status": status, "text": "", "final_url": None, "checked_at": _now(), "content_type": None}


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
            if _target(item.get("final_url"))[1] != hostname:
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
    "not_observed": "완료된 검색 기록에서 이 출처 URL을 확인하지 못했습니다.",
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


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Source verification cancelled")


def verify_web_sources(body, *, fetcher=None, cancel_event=None):
    """Verify retrieved quotes, or raise CancelledError without a partial result.

    An in-flight shared download retains its bounded deadline for other callers;
    cancellation is checked when it returns and before opening another source.
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
    _verify_sources(sources, verification, fetcher=fetcher, cancel_event=cancel_event)
    _check_cancelled(cancel_event)
    return result


def _verify_sources(sources, verification, *, fetcher=None, cancel_event=None):
    """Verify a flat list of web sources in place (quote_verified/verification_status/…), recording source_checks.

    Shared by the legacy fact_check path (``verify_web_sources``) and the items pipeline
    (``semantic_facts.verify_refutations``): only URLs observed by the provider's completed
    search are opened, at most MAX_SOURCE_URLS per call, within TOTAL_TIMEOUT.
    """
    observed = {_url(value) for value in verification.get("source_urls", []) if isinstance(value, str)} if isinstance(verification.get("source_urls"), list) else set()
    observed.discard(None)
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
        source.update(quote_verified=False, verification_reason="", verified_at=None, resolved_url=None)
        url = _url(source.get("url"))
        quote_text = _normalize(source.get("quote"))
        if not search_performed or not url or url not in observed:
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
                            if _target(item.get("final_url"))[1] != _target(url)[1]:
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
                verification["source_checks"].append(checks[url])
            item = cache[url]
            status = item.get("status", "fetch_error")
            if status == "ok":
                status = "verified" if quote_text in _normalize(item.get("text")) else "quote_not_found"
            checks[url]["quote_count"] += 1
            checks[url]["verified_quotes" if status == "verified" else "unverified_quotes"] += 1
            if status == "verified":
                source.update(quote_verified=True, verified_at=checks[url]["checked_at"], resolved_url=checks[url]["resolved_url"])
            elif checks[url]["resolved_url"]:
                source["resolved_url"] = checks[url]["resolved_url"]
        source["verification_status"] = status
        source["verification_reason"] = _REASONS.get(status, "출처 원문을 안전하게 수집할 수 없어 인용문을 확인하지 못했습니다.")
    _check_cancelled(cancel_event)
    return sources
