"""Explicit platform-mode entry; the legacy command remains separate."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from ..privacy import private_directory
from .app import CoreApp
from .config import validate_document
from .errors import PlatformError


def main(argv=None):
    parser = argparse.ArgumentParser(description="小希 Core 模块化平台")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[4] / "platform.example.json"))
    parser.add_argument("--data", required=True, help="本平台实例的独立运行数据目录")
    parser.add_argument("--host", help="覆盖配置中的监听地址，仅本次运行有效")
    parser.add_argument("--port", type=int, help="覆盖配置中的监听端口，仅本次运行有效")
    parser.add_argument("--auth-file", help="独立主人凭据文件；默认数据目录/owner.auth.json")
    parser.add_argument("--init-password-stdin", action="store_true", help="从标准输入读取一行密码，初始化凭据后退出")
    args = parser.parse_args(argv)
    from .auth import AuthError, AuthStore
    data_dir = Path(args.data).resolve()
    auth_path = Path(args.auth_file or os.environ.get("XIXI_PLATFORM_AUTH_FILE") or data_dir / "owner.auth.json").resolve()
    if args.init_password_stdin:
        private_directory(data_dir)
        password = sys.stdin.readline().rstrip("\r\n")
        try:
            AuthStore.initialize(auth_path, password=password)
        except (AuthError, OSError) as exc:
            print("主人凭据初始化失败：" + str(exc)[:500], file=sys.stderr)
            return 1
        finally:
            password = None
        print("主人凭据已初始化")
        return 0
    os.environ["XIXI_PLATFORM_AUTH_FILE"] = str(auth_path)
    core_app = server = None
    exit_status = 0
    try:
        from werkzeug.serving import WSGIRequestHandler, make_server
        from .http import create_app

        core_app = CoreApp(args.config, data_dir)
        http = core_app.config.snapshot()["http"]
        if args.host is not None:
            http["host"] = args.host
        if args.port is not None:
            http["port"] = args.port
        # Validate overrides without persisting them to the configuration file.
        document = core_app.config.snapshot()
        document["http"] = http
        validate_document(document)

        class QuietHandler(WSGIRequestHandler):
            def log_request(self, code="-", size="-"):
                pass

        flask_app = None

        def dispatch(environ, start_response):
            return flask_app(environ, start_response)

        server = make_server(http["host"], http["port"], dispatch, threaded=True, request_handler=QuietHandler)
        http["port"] = server.server_port
        core_app.config.runtime_http(http)
        flask_app = create_app(core_app)
        core_app.start()
        stopping = threading.Event()

        def stop(_signum, _frame):
            if not stopping.is_set():
                stopping.set()
                threading.Thread(target=server.shutdown, name="platform-http-shutdown", daemon=True).start()

        previous = {}
        for name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, stop)
        print(json.dumps({"mode": "platform", "host": http["host"], "port": server.server_port,
                          "origin": http.get("origin", "http://" + http["host"] + ":" + str(server.server_port)),
                          "data": str(data_dir)}, ensure_ascii=False), flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    except KeyboardInterrupt:
        pass
    except (AuthError, PlatformError, OSError, ValueError) as exc:
        print("平台启动或运行失败：" + type(exc).__name__ + ": " + str(exc)[:500], file=sys.stderr)
        exit_status = 1
    finally:
        # Admission is stopped first; normal module cleanup drains registered
        # calls before transport handles and the data-directory lock are closed.
        if core_app is not None:
            errors = core_app.close()
            if errors:
                print("退出时有模块资源未完成正常清理，请查看平台日志。", file=sys.stderr)
                exit_status = 1
        if server is not None:
            server.server_close()
    return exit_status
