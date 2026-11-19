"""Password authentication, owner sessions and optional gateway integration."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import threading
import time
from urllib.parse import urlsplit

from werkzeug.security import check_password_hash, generate_password_hash


SESSION_SECONDS = 7 * 24 * 60 * 60
GATEWAY_COOKIE_RE = re.compile(r"dsh-auth-[A-Za-z0-9_-]+=v1\.[A-Za-z0-9_.-]+")


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class AuthStore:
    """The single owner's verifier and bounded, in-memory login sessions."""

    def __init__(self, path: str | Path, clock=time.time):
        self.path = Path(path).resolve()
        self.clock = clock
        try:
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError()
            if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
                raise AuthError("auth_permissions", "登录凭据文件应仅允许运行用户读写。", 503)
            document = json.loads(self.path.read_text(encoding="utf-8"))
            verifier = document["password_hash"]
            if document.get("version") != 1 or not self._valid_verifier(verifier):
                raise ValueError()
        except FileNotFoundError:
            raise AuthError("auth_not_initialized", "尚未初始化主人密码，请先使用 --init-password-stdin。", 503) from None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise AuthError("auth_invalid", "登录凭据文件无效，请由外部检修恢复。", 503) from None
        self._verifier = verifier
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self._failures: dict[str, list[float]] = defaultdict(list)

    @staticmethod
    def _valid_verifier(value):
        return (isinstance(value, str) and len(value) <= 1024
                and value.startswith(("scrypt:", "pbkdf2:")) and len(value.split("$")) == 3)

    @classmethod
    def initialize(cls, path: str | Path, *, password=None, password_hash=None):
        """Create once. An existing owner hash can be imported without plaintext."""
        if (password is None) == (password_hash is None):
            raise AuthError("auth_initialization", "必须提供密码或已有密码摘要中的一项。")
        if password is not None:
            if not isinstance(password, str) or not 14 <= len(password) <= 256:
                raise AuthError("password_length", "主人密码需要 14 至 256 个字符。")
            password_hash = generate_password_hash(password, method="scrypt:32768:8:1")
        elif not cls._valid_verifier(password_hash):
            raise AuthError("auth_initialization", "已有密码摘要格式无效。")
        target = Path(path).absolute()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps({"version": 1, "password_hash": password_hash}, ensure_ascii=True).encode("utf-8")
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise AuthError("auth_exists", "登录凭据已经存在，初始化不会覆盖它。", 409) from None
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return target

    @staticmethod
    def _key(token):
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            return None
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune(self):
        now = self.clock()
        self._sessions = {key: value for key, value in self._sessions.items() if value["expires"] > now}
        self._failures = defaultdict(list, {
            peer: [at for at in values if at > now - 900]
            for peer, values in self._failures.items() if any(at > now - 900 for at in values)
        })

    def _new(self, authenticated=False):
        self._prune()
        if len(self._sessions) >= 4096:
            oldest = min(self._sessions, key=lambda key: self._sessions[key]["expires"])
            del self._sessions[oldest]
        token = secrets.token_urlsafe(32)
        item = {"authenticated": authenticated, "csrf": secrets.token_urlsafe(32),
                "expires": self.clock() + SESSION_SECONDS}
        self._sessions[self._key(token)] = item
        return token, dict(item)

    def session(self, token):
        with self._lock:
            self._prune()
            item = self._sessions.get(self._key(token))
            return dict(item) if item else None

    def new_session(self):
        with self._lock:
            return self._new()

    def authenticate(self, token, password, peer):
        if not isinstance(password, str) or len(password) > 256:
            raise AuthError("invalid_login", "密码不正确。", 401)
        with self._lock:
            self._prune()
            if len(self._failures[peer]) >= 10 or sum(map(len, self._failures.values())) >= 50:
                raise AuthError("login_rate_limited", "登录尝试过多，请稍后再试。", 429)
            try:
                accepted = check_password_hash(self._verifier, password)
            except (ValueError, TypeError):
                raise AuthError("auth_invalid", "登录凭据无法校验，请由外部检修恢复。", 503) from None
            if not accepted:
                self._failures[peer].append(self.clock())
                raise AuthError("invalid_login", "密码不正确。", 401)
            self._sessions.pop(self._key(token), None)
            self._failures.pop(peer, None)
            return self._new(authenticated=True)

    def logout(self, token):
        with self._lock:
            self._sessions.pop(self._key(token), None)
            return self._new()

    @staticmethod
    def verify_csrf(item, supplied):
        return bool(item and isinstance(supplied, str) and supplied.isascii()
                    and hmac.compare_digest(item["csrf"], supplied))


class OwnerGateway:
    """Verify gateway subrequests and delegate DSH sign-in to its private broker."""

    def __init__(self, origin, token, sso_socket, return_urls):
        self._token = token
        self._sso_socket = sso_socket
        self._return_urls = frozenset(return_urls)
        self._origin = urlsplit(origin)
        if token and (self._origin.scheme != "https" or not re.fullmatch(r"[a-f0-9]{64}", token)):
            raise AuthError("gateway_config_invalid", "网关认证需要 HTTPS 和有效的专用令牌配置。", 503)
        if sso_socket and (not Path(sso_socket).is_absolute() or "\0" in sso_socket):
            raise AuthError("gateway_config_invalid", "DSH 主人接入需要绝对套接字路径。", 503)
        for value in self._return_urls:
            try:
                parsed = urlsplit(value)
                valid = (value.isascii() and all(32 < ord(char) < 127 for char in value) and "\\" not in value
                         and parsed.scheme == "https" and parsed.hostname == self._origin.hostname and not parsed.fragment
                         and parsed.username is None and parsed.password is None and parsed.path.startswith("/")
                         and (parsed.port is None or 0 < parsed.port <= 65535))
            except ValueError:
                valid = False
            if not valid:
                raise AuthError("gateway_config_invalid", "登录回跳项需要同一主机的完整 HTTPS 地址。", 503)

    @classmethod
    def from_environment(cls, origin):
        return cls(origin, os.environ.get("XIXI_PLATFORM_GATEWAY_TOKEN"),
                   os.environ.get("XIXI_PLATFORM_GATEWAY_SSO_SOCKET"),
                   os.environ.get("XIXI_PLATFORM_GATEWAY_RETURN_URLS", "").split())

    @property
    def enabled(self):
        return bool(self._token)

    def verify_token(self, supplied):
        if not self.enabled:
            raise AuthError("gateway_disabled", "网关认证未配置。", 404)
        if not supplied.isascii() or not hmac.compare_digest(self._token, supplied):
            raise AuthError("gateway_forbidden", "网关认证失败。", 403)

    def login_destination(self, return_to, next_path):
        if return_to is not None:
            return return_to if return_to in self._return_urls else "/"
        if next_path == "/dsh/":
            return next((url for url in self._return_urls
                         if urlsplit(url).port in {None, 443} and urlsplit(url).path == "/dsh/"
                         and not urlsplit(url).query), "/")
        return "/"

    @staticmethod
    def owner_session_id(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _request_sso(self, op, **fields):
        try:
            if not self._sso_socket:
                raise ValueError()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(2)
                channel.connect(self._sso_socket)
                channel.sendall((json.dumps({"op": op, **fields}) + "\n").encode("utf-8"))
                with channel.makefile("rb") as stream:
                    line = stream.readline(4097)
            if len(line) > 4096 or not line.endswith(b"\n"):
                raise ValueError()
            result = json.loads(line)
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError()
            return result
        except (OSError, ValueError, AttributeError):
            raise AuthError("dsh_sign_in_unavailable", "DSH 主人登录暂不可用。", 503) from None

    def dsh_cookie(self):
        value = self._request_sso("cookie").get("cookie")
        if not isinstance(value, str) or not GATEWAY_COOKIE_RE.fullmatch(value):
            raise AuthError("dsh_sign_in_unavailable", "DSH 主人登录暂不可用。", 503)
        return value

    def revoke(self, token):
        if not self._token or not self._sso_socket:
            return
        if self._request_sso("revoke", session=self.owner_session_id(token)).get("revoked") is not True:
            raise AuthError("dsh_sign_in_unavailable", "DSH 主人登录暂不可用。", 503)
