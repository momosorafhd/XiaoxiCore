"""All business plugins on production entry points with an isolated OneBot peer.

The default model adapter is local. XIXI_FIXTURE_SYNTHETIC_DSH=1 selects only
the stage 3/4 loopback DSH fixture, whose provider returns scripted responses.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import socket
import tempfile
import threading
import time
import unittest

from test_platform_message_bridge import ROOT, Peer, event, wait_for
from xixi_core.platform.app import CoreApp
from xixi_core.platform.http import create_app
from xixi_core.platform.auth import AuthStore


class BusinessFlowTests(unittest.TestCase):
    def test_owner_input_restart_boundary_and_independent_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "reverse.token"
            token = os.urandom(24).hex()
            secret.write_text(token, encoding="ascii")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            channel_config = {"mode":"onebot_reverse", "listenHost":"127.0.0.1", "listenPort":port,
                              "allowedPeerIp":"127.0.0.1", "ownerId":"11", "botId":"22",
                              "reverseTokenFile":str(secret)}
            real_dsh = os.environ.get("XIXI_FIXTURE_SYNTHETIC_DSH") == "1"
            dsh_config = {"mode":"dsh", "url":"http://127.0.0.1:18866"} if real_dsh else {"mode":"mock"}
            plugins = {"library":{}, "message_bridge":channel_config, "dsh_bridge":dsh_config,
                       "assistant":{"automaticDream":False,"automaticHandoff":False}, "console":{}}
            path = root / "platform.json"
            path.write_text(json.dumps({"plugins_dir":str(ROOT/"plugins"), "modules":{
                key:{"enabled":True,"config":value} for key,value in plugins.items()}}), encoding="utf-8")
            pulls, replies = [], []
            allow_history = threading.Event()
            with CoreApp(path, root/"data") as app:
                startup = app.manager.list()
                for item in startup:
                    if item["id"] in plugins:
                        self.assertEqual(item["state"],"running",item.get("error"))
                assistant = app.services.get("assistant")
                channel = app.services.get("message_bridge")
                dsh = app.services.get("dsh_bridge")
                auth = app.data_dir / "fixture.auth"
                AuthStore.initialize(auth, password=os.urandom(24).hex())
                http = create_app(app, auth_store=AuthStore(auth)).test_client()
                self.assertEqual(http.post("/api/bridge/tool", json={}, headers={
                    "X-Forwarded-For":"192.0.2.1"}).status_code, 403)
                self.assertEqual(http.post("/api/bridge/tool", json={}).status_code, 401)
                stamp = int(time.time())
                initial = event(41, stamp, text="synthetic first turn")
                offline = event(43, stamp, text="synthetic offline input")
                def onebot(action, params):
                    if action == "get_login_info":
                        return {"user_id":22}
                    if action == "get_friend_msg_history":
                        pulls.append(params)
                        if not allow_history.wait(4):
                            raise AssertionError("missing realtime interleave")
                        return {"messages":[event(18,stamp-30), initial, offline]}
                    self.assertEqual(action, "send_private_msg")
                    self.assertEqual(params["user_id"],11)
                    self.assertTrue(params["message"][0]["data"]["text"])
                    replies.append(params["message"][0]["data"]["text"])
                    return {"message_id":100 + len(replies)}
                peer = Peer(port,token,onebot)
                try:
                    wait_for(lambda:channel.status()["connected"])
                    assistant.start_receiving()
                    wait_for(lambda:assistant.status()["startup_unread"]["state"]=="first_start")
                    self.assertEqual(pulls,[])
                    peer.send(initial)
                    peer.send(initial)
                    wait_for(lambda:len(replies)==1 and not assistant.status()["activity"],30)
                    original_session = assistant.status()["session_id"]
                    self.assertTrue(original_session)
                    self.assertIsNone(assistant.status()["last_error"])
                    self.assertEqual(assistant.stop_receiving()["state"],"not_started")
                    self.assertEqual(dsh.open_session(original_session), original_session)
                    # Window changes even if a slow scripted DSH turn spans multiple seconds.
                    offline["time"] = int(time.time())
                    peer.send(offline)
                    assistant.start_receiving()
                    wait_for(lambda:pulls)
                    # Identical channel id appears in live WS and in the history response.
                    peer.send(offline)
                    peer.send(event(44,offline["time"],text="synthetic live input"))
                    wait_for(lambda:assistant.status()["queue_depth"]>=2)
                    allow_history.set()
                    wait_for(lambda:len(replies)>=3 and not assistant.status()["activity"],40)
                    state = assistant.status()
                    self.assertEqual(len(replies),3)
                    self.assertEqual(state["startup_unread"]["count"],1)
                    self.assertEqual(len(pulls),1)
                    self.assertEqual(state["session_id"],original_session)
                    self.assertIsNone(state["last_error"])
                    self.assertEqual(app.manager.stop("library")["state"],"stopped")
                    self.assertEqual(assistant.status()["state"],"running")
                    self.assertEqual(app.manager.stop("console")["state"],"stopped")
                    self.assertEqual(dsh.open_session(original_session),original_session)
                    self.assertEqual(app.manager.start("library")["state"],"running")
                    self.assertEqual(app.manager.start("console")["state"],"running")
                    for name in ("library","dsh_bridge","message_bridge","assistant"):
                        self.assertEqual(app.manager.reload(name)["state"],"running",name)
                    self.assertFalse(peer.errors, peer.errors)
                    if not real_dsh:
                        self.assertEqual([v.split('：')[-1] for v in replies],
                                         ["synthetic first turn","synthetic offline input","synthetic live input"])
                    evidence = os.environ.get("XIXI_FIXTURE_FLOW_EVIDENCE")
                    if evidence:
                        Path(evidence).write_text(json.dumps({
                            "dsh":"isolated DSH with synthetic model" if real_dsh else "MockRuntime",
                            "channel":"isolated reverse OneBot socket", "session":original_session,
                            "historyQueries":len(pulls),"historyAccepted":state["startup_unread"]["count"],
                            "replies":len(replies),"sessionPreserved":True,
                            "sequence":["first turn","startup history","normal live"],
                            "moduleReloaded":["library","dsh_bridge","message_bridge","assistant"]
                        },ensure_ascii=False,indent=2),encoding="utf-8")
                finally:
                    allow_history.set()
                    peer.close()


    def test_abrupt_process_exit_uses_estimated_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = {"message_bridge":{"mode":"offline"}, "dsh_bridge":{"mode":"mock"},
                       "assistant":{"startOnEnable":True, "heartbeatSeconds":1}}
            config = root/"config.json"
            config.write_text(json.dumps({"plugins_dir":str(ROOT/"plugins"), "modules":{
                key:{"enabled":True,"config":value} for key,value in modules.items()}}),encoding="utf-8")
            child = """import os, sys, time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'packages/xixi-core'))
from xixi_core.platform.app import CoreApp
app = CoreApp(sys.argv[1],sys.argv[2])
app.start()
service = app.services.get('assistant')
for step in range(500):
    status = service.status()
    if (status['startup_unread'] or {}).get('state') == 'first_start':
        os._exit(23)  # Simulate loss of the entire Core process without shutdown hooks.
    time.sleep(.01)
raise RuntimeError('assistant did not enter first start')
"""
            result = subprocess.run([sys.executable,"-c",child,str(config),str(root/"data")],
                                    cwd=ROOT, capture_output=True, timeout=15)
            self.assertEqual(result.returncode,23,result.stderr.decode(errors="replace"))
            with CoreApp(config,root/"data") as app:
                service = app.services.get("assistant")
                wait_for(lambda:(service.status()["startup_unread"] or {}).get("state")=="failed")
                recovery = service.status()["startup_unread"]
                self.assertTrue(recovery["window"]["estimated"])
                self.assertGreater(recovery["window"]["stopped_at"],0)
                self.assertEqual(service.status()["state"],"running")
                self.assertIsNone(service.status()["session_id"])
