"""Shared owner authentication for nginx subrequests and the private DSH broker."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "xixi-core"))

from xixi_core.platform.app import CoreApp
from xixi_core.platform.auth import AuthError, AuthStore, OwnerGateway, SESSION_SECONDS
from xixi_core.platform.http import create_app


HOST = "10.50.0.200"
ORIGIN = f"https://{HOST}:18911"
DSH = f"https://{HOST}/dsh/"
DESKTOP = f"https://{HOST}:16081/vnc.html?autoconnect=true&resize=scale"
GATEWAY_TOKEN = "ab" * 32
PASSWORD = "isolated-gateway-owner-password"
COOKIE = "__Host-xixi_platform"
BROKER_COOKIE = "dsh-auth-fixture=v1.synthetic.private.authentication"
QQ_AUTH = "/_qq-owner-auth"
DSH_AUTH = "/_gateway-auth"


class GatewayHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="xixi-platform-gateway-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / "platform.json"
        config.write_text(json.dumps({"http": {"origin": ORIGIN, "trusted_proxy": "127.0.0.1",
                                                 "allowed_networks": ["10.50.0.0/24"]},
                                      "modules": {"console": {"enabled": True}}}), encoding="utf-8")
        data = root / "data"
        verifier = AuthStore.initialize(data / "owner.auth.json", password=PASSWORD)
        self.now = 1_795_000_000.0
        store = AuthStore(verifier, clock=lambda: self.now)
        self.core = CoreApp(config, data)
        self.addCleanup(self.core.close)
        self.core.start()
        self.environment = {"XIXI_PLATFORM_GATEWAY_TOKEN": GATEWAY_TOKEN,
                            "XIXI_PLATFORM_GATEWAY_SSO_SOCKET": str(root / "sso.sock"),
                            "XIXI_PLATFORM_GATEWAY_RETURN_URLS": f"{DSH} {DESKTOP}"}
        with patch.dict(os.environ, self.environment):
            self.app = create_app(self.core, auth_store=store)
        self.client = self.app.test_client()
        self.forwarded = {"X-Forwarded-For": "10.50.0.2", "X-Forwarded-Proto": "https"}

    def browser(self, path, method="GET", body=None, csrf=None, origin=ORIGIN, **other):
        headers = dict(self.forwarded, Origin=origin)
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return self.client.open(path, method=method, json=body, headers=headers, base_url=ORIGIN, **other)

    def auth_request(self, path, *, token=GATEWAY_TOKEN, address="127.0.0.1", **extra):
        headers = dict(self.forwarded, **extra)
        if token is not None:
            headers["X-Xixi-Gateway-Token"] = token
        return self.client.get(path, headers=headers, base_url=ORIGIN, environ_overrides={"REMOTE_ADDR": address})

    def login(self):
        csrf = self.browser("/v1/session").json["csrf"]
        result = self.browser("/v1/login", "POST", {"password": PASSWORD}, csrf)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json["authenticated"])
        return result

    def test_owner_and_private_gateway_proof_are_both_required(self):
        with patch.object(OwnerGateway, "_request_sso", return_value={"cookie": BROKER_COOKIE}) as broker:
            for target in (QQ_AUTH, DSH_AUTH):
                self.assertEqual(self.auth_request(target).status_code, 401)
            broker.assert_not_called()
            logged = self.login()
            cookie = logged.headers["Set-Cookie"]
            for flag in ("Secure", "HttpOnly", "Path=/", "SameSite=Strict"):
                self.assertIn(flag, cookie)
            self.assertNotIn("Domain=", cookie)
            self.assertEqual(self.auth_request(DSH_AUTH, token=None).status_code, 403)
            self.assertEqual(self.auth_request(DSH_AUTH, token="00" * 32).status_code, 403)
            self.assertEqual(self.auth_request(DSH_AUTH, address="10.50.0.7").status_code, 403)
            self.assertEqual(self.auth_request(DSH_AUTH, Origin=ORIGIN).status_code, 403)
            self.assertEqual(self.auth_request(DSH_AUTH, **{"X-Forwarded-For": "10.50.0.2, 127.0.0.1"}).status_code, 403)
            self.assertEqual(self.auth_request(DSH_AUTH, **{"X-Forwarded-For": "198.51.100.2"}).status_code, 403)
            qq = self.auth_request(QQ_AUTH)
            self.assertEqual(qq.status_code, 204)
            self.assertNotIn("X-Xixi-DSH-Cookie", qq.headers)
            broker.assert_not_called()

            dsh = self.auth_request(DSH_AUTH)
            self.assertEqual(dsh.status_code, 204)
            self.assertEqual(dsh.headers["X-Xixi-DSH-Cookie"], BROKER_COOKIE)
            token = self.client.get_cookie(COOKIE, domain=HOST).value
            self.assertEqual(dsh.headers["X-Xixi-Owner-Session"], hashlib.sha256(token.encode()).hexdigest())
            self.assertEqual(float(dsh.headers["X-Xixi-Owner-Expires"]) - self.now, SESSION_SECONDS)
            self.assertNotIn("Set-Cookie", dsh.headers)
            broker.assert_called_once_with("cookie")
            self.now += SESSION_SECONDS + 1
            self.assertEqual(self.auth_request(DSH_AUTH).status_code, 401)
            broker.assert_called_once()

    def test_login_targets_are_exact_and_legacy_next_is_preserved(self):
        rendered = self.browser("/login", query_string={"return_to": DESKTOP})
        self.assertIn('data-destination="' + DESKTOP.replace("&", "&amp;") + '"', rendered.get_data(as_text=True))
        rendered = self.browser("/login", query_string={"next": "/dsh/"})
        self.assertIn('data-destination="' + DSH + '"', rendered.get_data(as_text=True))
        self.login()
        redirect = self.browser("/login", query_string={"return_to": DESKTOP})
        self.assertEqual(redirect.status_code, 303)
        self.assertEqual(redirect.headers["Location"], DESKTOP)
        for target in ("//outside.test/", "https://outside.test/", "javascript:alert(1)",
                       DSH + "?change=1", DSH.replace(HOST, HOST + "@outside.test"),
                       DESKTOP.replace("autoconnect=true", "autoconnect=false"), "/qq/"):
            with self.subTest(target=target):
                self.assertEqual(self.browser("/login", query_string={"return_to": target}).headers["Location"], "/")

    def test_fresh_gateway_login_and_its_assets_survive_console_stop(self):
        login_query = {"return_to": DESKTOP}
        original_page = self.browser("/login", query_string=login_query).data
        login_assets = ("/static/login.js", "/static/style.css")
        original_assets = {path: self.browser(path).data for path in login_assets}
        self.assertEqual(self.core.manager.stop("console")["state"], "stopped")
        self.client = self.app.test_client()

        self.assertEqual(self.auth_request(QQ_AUTH).status_code, 401)
        page = self.browser("/login", query_string=login_query)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data, original_page)
        for path in login_assets:
            with self.subTest(path=path):
                response = self.browser(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, original_assets[path])

        csrf = self.login().json["csrf"]
        self.assertEqual(self.browser("/login", query_string=login_query).headers["Location"], DESKTOP)
        self.assertEqual(self.auth_request(QQ_AUTH).status_code, 204)
        with patch.object(OwnerGateway, "_request_sso", return_value={"cookie": BROKER_COOKIE}):
            self.assertEqual(self.auth_request(DSH_AUTH).headers["X-Xixi-DSH-Cookie"], BROKER_COOKIE)
        for path in ("/", "/modules", "/library", "/static/common.js", "/static/library.css"):
            with self.subTest(path=path):
                self.assertEqual(self.browser(path).status_code, 503)

        started = self.browser("/api/platform/modules/console/start", "POST", {}, csrf)
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json["item"]["state"], "running")
        self.assertEqual(self.browser("/").status_code, 200)
        self.assertEqual(self.browser("/static/common.js").status_code, 200)

    def test_logout_rejects_cross_origin_and_revokes_old_session(self):
        csrf = self.login().json["csrf"]
        cookie = self.client.get_cookie(COOKIE, domain=HOST).value
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        with patch.object(OwnerGateway, "_request_sso", return_value={"revoked": True}) as broker:
            self.assertEqual(self.browser("/v1/logout", "POST", {}, csrf, origin=DSH.rstrip("/")).status_code, 403)
            self.assertEqual(self.browser("/v1/logout", "POST", {}, "wrong").status_code, 403)
            broker.assert_not_called()
            result = self.browser("/v1/logout", "POST", {}, csrf)
            self.assertEqual(result.status_code, 200)
            self.assertFalse(result.json["authenticated"])
            broker.assert_called_once_with("revoke", session=digest)
        self.client.set_cookie(COOKIE, cookie, domain=HOST)
        self.assertEqual(self.auth_request(DSH_AUTH).status_code, 401)
        self.assertEqual(self.auth_request(QQ_AUTH).status_code, 401)

    def test_broker_failure_returns_no_dsh_identity_and_qq_still_works(self):
        self.login()
        with patch.object(OwnerGateway, "_request_sso", return_value={"cookie": "invalid\nInjected: yes"}):
            failed = self.auth_request(DSH_AUTH)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("X-Xixi-DSH-Cookie", failed.headers)
        self.assertNotIn("X-Xixi-Owner-Session", failed.headers)
        self.assertEqual(self.auth_request(QQ_AUTH).status_code, 204)


class BrokerProtocolTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires a local Unix domain socket")
    def test_private_unix_socket_cookie_and_revoke_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="xg-", dir=Path(tempfile.gettempdir())) as folder:
            address = str(Path(folder) / "s")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(address)
                server.listen()
                server.settimeout(3)
                calls = []

                def run():
                    for reply in ({"cookie": BROKER_COOKIE}, {"revoked": True}):
                        connection, _ = server.accept()
                        with connection, connection.makefile("rb") as stream:
                            calls.append(json.loads(stream.readline(4097)))
                            connection.sendall((json.dumps(reply) + "\n").encode())

                worker = Thread(target=run, daemon=True)
                worker.start()
                gateway = OwnerGateway(ORIGIN, GATEWAY_TOKEN, address, [DSH])
                self.assertEqual(gateway.dsh_cookie(), BROKER_COOKIE)
                gateway.revoke("test-owner-token")
                worker.join(3)
                self.assertEqual(calls, [{"op": "cookie"}, {"op": "revoke", "session":
                                      hashlib.sha256(b"test-owner-token").hexdigest()}])

    def test_gateway_configuration_rejects_redirects_outside_the_owner_host(self):
        for target in ("http://10.50.0.200/dsh/", "https://other.example/dsh/", "https://10.50.0.200@other.example/dsh/"):
            with self.assertRaises(AuthError):
                OwnerGateway(ORIGIN, GATEWAY_TOKEN, None, [target])
