"""Exercise both sample Nginx sites with temporary credentials, TLS and loopback peers."""
import argparse
from contextlib import ExitStack, closing
from hashlib import sha256
from http.client import HTTPSConnection
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from unittest.mock import patch

from werkzeug.serving import make_server
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/xixi-core"))
from xixi_core.platform import CoreApp
from xixi_core.platform.auth import AuthStore
from xixi_core.platform.http import create_app

def serve(stack, server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def stop():
        server.shutdown()
        thread.join(5)
        server.server_close()
    stack.callback(stop)
    return server.server_port

def run(work, stack, node):
    os.chmod(work, 0o700)
    def start(args):
        child = subprocess.Popen(args, cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            child.terminate()
            child.wait(timeout=8)
        stack.callback(stop)
        return child

    with closing(socket.socket()) as a, closing(socket.socket()) as b:
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        core_port, dsh_port = a.getsockname()[1], b.getsockname()[1]
    domain = "assistant.home.arpa"
    core_host, dsh_host = f"{domain}:{core_port}", f"{domain}:{dsh_port}"
    origin = "https://" + core_host
    dsh_origin = "https://" + dsh_host
    token, password = secrets.token_hex(32), secrets.token_urlsafe(30)
    ipc = work / "owner.sock"
    # Exercise the actual distributed SSO plugin with the two DSH Web APIs it calls.
    fixture = work / "native-web-fixture.mjs"
    fixture.write_text("""import {EventEmitter} from 'node:events';
import {apply} from %s;
await apply({webServer:{server:new EventEmitter()}, effect(factory){factory();},
 connection:{authenticatedUrl(v){return v+'?fixture=1'},authorizeIndex(req,res){
 res.writeHead(303,{'set-cookie':'dsh-auth-local=v1.fixture; HttpOnly'});res.end();}}},
 {origin:%s,socketPath:%s});
""" % tuple(json.dumps(v) for v in ((ROOT/"integrations/owner-sso/plugin.mjs").as_uri(), dsh_origin, str(ipc))))
    broker = start([node, str(fixture)])
    until = time.monotonic() + 5
    while not ipc.exists() and broker.poll() is None and time.monotonic() < until:
        time.sleep(.05)
    assert ipc.exists(), "SSO fixture failed to bind"

    stack.enter_context(patch.dict(os.environ, {"XIXI_PLATFORM_GATEWAY_TOKEN":token,
        "XIXI_PLATFORM_GATEWAY_SSO_SOCKET":str(ipc),"XIXI_PLATFORM_GATEWAY_RETURN_URLS":dsh_origin + "/"}))
    os.environ.pop("XIXI_PLATFORM_AUTH_FILE", None)
    (work/"gateway.inc").write_text(f'proxy_set_header X-Xixi-Gateway-Token "{token}";\n')
    config, data = work/"platform.json", work/"data"
    config.write_text(json.dumps({"http":{"origin":origin,"trusted_proxy":"127.0.0.1","allowed_networks":["127.0.0.0/8"]},
        "plugins_dir":str(ROOT/"plugins"),"modules":{"console":{"enabled":True}}}))
    AuthStore.initialize(data/"owner.auth.json", password=password)
    core = stack.enter_context(CoreApp(config,data))
    backend = serve(stack, make_server("127.0.0.1",0,create_app(core),threaded=True))
    seen = []
    class Portal(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            seen.append(self.headers)
            self.send_response(200)
            self.end_headers()
    portal = serve(stack, ThreadingHTTPServer(("127.0.0.1",0),Portal))

    cert, key = work/"qa.crt", work/"qa.key"
    subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=localhost",
                    "-addext","subjectAltName=IP:127.0.0.1","-out",str(cert),"-keyout",str(key)],check=True,capture_output=True)
    templates = ROOT/"examples/lan"
    sites = "\n".join((templates/f).read_text() for f in ("core.nginx.conf","dsh-sso.nginx.conf"))
    replacements = {"listen 443 ssl": f"listen 127.0.0.1:{core_port} ssl",
                    "listen 9443 ssl": f"listen 127.0.0.1:{dsh_port} ssl",
                    "Host assistant.home.arpa;": f"Host {core_host};",
                    "assistant.home.arpa:9443":dsh_host,"assistant.home.arpa%3A9443":f"{domain}%3A{dsh_port}",
                    "https://assistant.home.arpa/login":origin+"/login",
                    "127.0.0.1:18910":f"127.0.0.1:{backend}", "127.0.0.1:3080":f"127.0.0.1:{portal}",
                    "/etc/nginx/tls/assistant.crt":str(cert),"/etc/nginx/tls/assistant.key":str(key),
                    "/etc/nginx/private/xiaoxi-gateway.inc":str(work/"gateway.inc")}
    for old, new in replacements.items(): sites = sites.replace(old,new)
    # Keep all Nginx temporary files within the temporary test directory.
    temps = "\n".join(f"{kind}_temp_path {work}/{kind};" for kind in ("client_body","proxy","fastcgi","uwsgi","scgi"))
    conf = work/"nginx.conf"
    conf.write_text(f"pid {work}/nginx.pid;\nerror_log {work}/error.log;\nevents {{}}\nhttp {{\n{temps}\n{sites}\n}}\n")
    args = [shutil.which("nginx") or "/usr/sbin/nginx","-p",str(work),"-c",str(conf)]
    subprocess.run([*args,"-t"],check=True,capture_output=True)
    child = start([*args,"-g","daemon off;"])
    tls = ssl.create_default_context(cafile=str(cert))
    cookies = {}
    def request(port, path, body=None, **headers):
        conn = HTTPSConnection("127.0.0.1",port,context=tls,timeout=3)
        fields = {"Host":core_host if port == core_port else dsh_host,
                  "Cookie":"; ".join(k+"="+v for k,v in cookies.items()), **headers}
        if body is not None: fields.update({"Content-Type":"application/json","Origin":origin})
        try:
            conn.request("GET" if body is None else "POST",path,None if body is None else json.dumps(body),headers=fields)
            response = conn.getresponse()
            for header in response.headers.get_all("Set-Cookie", []):
                cookies.update({k:v.value for k,v in SimpleCookie(header).items()})
            return response.status, response.read(), response.getheader("Location")
        finally: conn.close()
    until = time.monotonic() + 5
    while True:
        try:
            assert request(core_port,"/health")[0] == 200
            break
        except OSError:
            if time.monotonic()>until or child.poll() is not None: raise
            time.sleep(.05)
    status, _, location = request(dsh_port,"/")
    assert status == 303 and location.startswith(origin + "/login?return_to=")
    assert request(dsh_port,"/api/session/list")[0] == 401
    for path in ("/_gateway-auth","/_qq-owner-auth","/api/bridge/tool"):
        assert request(core_port,path)[0] == 404
    csrf = json.loads(request(core_port,"/api/auth/session")[1])["csrf"]
    status, body, _ = request(core_port,"/api/auth/login",{"password":password},**{"X-CSRF-Token":csrf})
    assert status == 200
    csrf = json.loads(body)["csrf"]
    assert request(dsh_port,"/")[0] == 200
    assert seen[-1]["Cookie"] == "dsh-auth-local=v1.fixture"
    assert seen[-1]["X-Xixi-Owner-Session"] == sha256(cookies["__Host-xixi_platform"].encode()).hexdigest()
    assert not seen[-1].get("X-Xixi-Gateway-Token")
    assert request(core_port,"/api/auth/logout",{},**{"X-CSRF-Token":csrf})[0] == 200
    assert request(dsh_port,"/")[0] == 303
    print("PASS: loopback TLS, private routes, anonymous/login/logout, native cookie and owner headers")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="xiaoxi-lan-") as directory, ExitStack() as stack:
        run(Path(directory),stack,args.node)
