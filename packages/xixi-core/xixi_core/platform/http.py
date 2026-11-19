"""Stable HTTP transport and owner boundary for the platform host.

The owner boundary follows the prior console's exact origin, CSRF and explicit
LAN proxy policy. The module registry is queried for every call; no plugin
changes Flask's routing table or owns authentication.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import traceback
from urllib.parse import urlsplit

from flask import Flask, Response, g, jsonify, redirect, request
from werkzeug.exceptions import HTTPException

from ..modules.console import ConsoleAssets, LOGIN_ASSET_NAMES
from .auth import AuthError, AuthStore, OwnerGateway, SESSION_SECONDS
from .errors import PlatformError


def _ip(value):
    try:
        parsed = ipaddress.ip_address(value)
        return parsed.ipv4_mapped if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped else parsed
    except (ValueError, TypeError):
        raise AuthError("invalid_peer", "访问来源无效。", 403) from None


def create_app(core_app, *, auth_store=None):
    settings = core_app.config.snapshot().get("http", {})
    origin = settings.get("origin") or "http://127.0.0.1:" + str(settings.get("port", 18910))
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise AuthError("invalid_origin", "http.origin 必须是完整的访问来源，不带路径。")
    if parsed.scheme == "http" and not _ip(parsed.hostname).is_loopback:
        raise AuthError("https_required", "局域网管理入口需要 HTTPS；HTTP 只用于本机调试。")
    proxy = _ip(settings["trusted_proxy"]) if settings.get("trusted_proxy") else None
    try:
        networks = tuple(ipaddress.ip_network(value, strict=True)
                         for value in settings.get("allowed_networks", ["127.0.0.0/8", "::1/128"]))
        if not networks:
            raise ValueError()
    except (TypeError, ValueError):
        raise AuthError("invalid_networks", "http.allowed_networks 必须是有效的局域网网段列表。") from None
    allowed_lan = tuple(ipaddress.ip_network(value) for value in
                        ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128"))
    if any(not any(network.version == lan.version and network.subnet_of(lan) for lan in allowed_lan)
           for network in networks):
        raise AuthError("invalid_networks", "管理入口仅允许配置本机或局域网网段。")
    store = auth_store or AuthStore(Path(os.environ.get("XIXI_PLATFORM_AUTH_FILE", ""))
                                   if os.environ.get("XIXI_PLATFORM_AUTH_FILE")
                                   else core_app.data_dir / "owner.auth.json")
    gateway = OwnerGateway.from_environment(origin)
    gateway_login = ConsoleAssets(pages=("login",), asset_names=LOGIN_ASSET_NAMES) if gateway.enabled else None
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=256 * 1024, JSON_SORT_KEYS=False)
    app.json.ensure_ascii = False
    cookie_name = "__Host-xixi_platform" if parsed.scheme == "https" else "xixi_platform_local"
    app.extensions.update(platform=core_app, owner_auth=store)
    logger = core_app.logs.logger("http")

    def set_cookie(response, token):
        response.set_cookie(cookie_name, token, max_age=SESSION_SECONDS,
                            secure=parsed.scheme == "https", httponly=True, samesite="Strict", path="/")
        return response

    @app.before_request
    def owner_boundary():
        if request.endpoint == "bridge_tool":
            if (not _ip(request.remote_addr).is_loopback or request.headers.get("Origin")
                    or any(name.lower() == "forwarded" or name.lower().startswith("x-forwarded-")
                           for name, _ in request.headers)):
                raise AuthError("bridge_local_only", "工具回调仅接受 DSH 的本机直连。", 403)
            bridge = core_app.services.get("dsh_bridge")
            bridge.authenticate_tool(request.headers.get("Authorization", ""))
            # One work artifact may contain one million Unicode characters.
            request.max_content_length = 5 * 1024 * 1024
            g.tool_bridge = bridge
            return
        if request.host.lower() != parsed.netloc.lower():
            raise AuthError("host_forbidden", "访问地址与配置的管理入口不一致。", 403)
        peer = _ip(request.remote_addr)
        if request.endpoint in {"qq_owner_auth", "gateway_auth"}:
            if proxy is None or not peer.is_loopback or peer != proxy or request.headers.get("Origin"):
                raise AuthError("gateway_local_only", "网关认证仅接受可信本机代理的内部请求。", 403)
            gateway.verify_token(request.headers.get("X-Xixi-Gateway-Token", ""))
        scheme = request.scheme
        if proxy is not None:
            if peer != proxy:
                raise AuthError("proxy_forbidden", "请求未通过指定管理入口。", 403)
            forwarded = request.headers.get("X-Forwarded-For", "")
            if "," in forwarded:
                raise AuthError("proxy_forbidden", "管理入口的来源信息无效。", 403)
            peer = _ip(forwarded)
            scheme = request.headers.get("X-Forwarded-Proto", "")
        if scheme != parsed.scheme:
            raise AuthError("scheme_forbidden", "访问协议与管理入口不一致。", 403)
        if not any(peer.version == net.version and peer in net for net in networks):
            raise AuthError("network_forbidden", "此网络不能访问管理台。", 403)
        g.owner_peer = str(peer)
        g.owner_token = request.cookies.get(cookie_name)
        g.owner_session = store.session(g.owner_token)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("Origin") != origin:
                raise AuthError("origin_forbidden", "操作来源与管理入口不一致。", 403)
            if not store.verify_csrf(g.owner_session, request.headers.get("X-CSRF-Token")):
                raise AuthError("csrf_failed", "页面会话已失效，请刷新后重试。", 403)
        public = request.path in {"/login", "/health", "/healthz", "/api/auth/session", "/api/auth/login", "/v1/session", "/v1/login"}
        public = public or request.path.startswith("/static/")
        if not public and not (g.owner_session and g.owner_session["authenticated"]):
            if request.path in {"/", "/modules", "/logs", "/status", "/status/history", "/assistant", "/library", "/prompts", "/models", "/persona", "/injections"}:
                return redirect("/login", code=303)
            raise AuthError("login_required", "请先登录主人账号。", 401)

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def error_response(error):
        return jsonify(success=False, error={"code": error.code, "message": str(error)}), error.status

    app.register_error_handler(AuthError, error_response)
    app.register_error_handler(PlatformError, error_response)

    @app.errorhandler(HTTPException)
    def http_error(error):
        descriptions = {400: "请求格式无效。", 404: "入口不存在。", 405: "此入口不支持该操作。", 413: "请求内容过大。"}
        return jsonify(success=False, error={"code": "http_error", "message": descriptions.get(error.code, "请求无法完成。")}), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        frames = traceback.extract_tb(error.__traceback__)
        location = (Path(frames[-1].filename).name + ":" + str(frames[-1].lineno)) if frames else "unknown"
        route = str(request.url_rule) if request.url_rule is not None else "unmatched"
        logger.error("平台 HTTP 请求失败：%s %s（%s，%s）", request.method, route, type(error).__name__, location)
        return jsonify(success=False, error={"code": "internal_error", "message": "操作失败，请查看平台日志。"}), 500

    @app.get("/health")
    @app.get("/healthz")
    def health():
        return jsonify(ok=True, name="XiaoxiCore Platform")

    @app.get("/api/auth/session")
    @app.get("/v1/session")
    def owner_session():
        if g.owner_session:
            return jsonify(g.owner_session)
        token, item = store.new_session()
        return set_cookie(jsonify(item), token)

    @app.post("/api/auth/login")
    @app.post("/v1/login")
    def owner_login():
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"password"}:
            raise AuthError("invalid_login", "请提供主人密码。", 400)
        token, item = store.authenticate(g.owner_token, body["password"], g.owner_peer)
        revoke_previous_gateway_session()
        return set_cookie(jsonify(item), token)

    @app.post("/api/auth/logout")
    @app.post("/v1/logout")
    def owner_logout():
        token, item = store.logout(g.owner_token)
        revoke_previous_gateway_session()
        return set_cookie(jsonify(item), token)

    def revoke_previous_gateway_session():
        if g.owner_session and g.owner_session["authenticated"]:
            try:
                gateway.revoke(g.owner_token)
            except AuthError:
                logger.warning("主人会话已结束，DSH 网关暂未确认撤销。")

    @app.get("/_qq-owner-auth")
    def qq_owner_auth():
        return Response(status=204)

    @app.get("/_gateway-auth")
    def gateway_auth():
        dsh_cookie = gateway.dsh_cookie()
        if not store.session(g.owner_token):
            raise AuthError("login_required", "请先登录主人账号。", 401)
        return Response(status=204, headers={
            "X-Xixi-DSH-Cookie": dsh_cookie,
            "X-Xixi-Owner-Session": gateway.owner_session_id(g.owner_token),
            "X-Xixi-Owner-Expires": str(g.owner_session["expires"]),
        })

    def console_page(name, **context):
        content, content_type = core_app.services.get("console.web").page(name, request, **context)
        return Response(content, content_type=content_type)

    @app.get("/")
    def index():
        return console_page("overview")

    @app.get("/modules")
    def modules_page():
        return console_page("modules")

    @app.post("/api/bridge/tool")
    def bridge_tool():
        return jsonify(g.tool_bridge.handle_tool(request.get_json()))

    @app.get("/library")
    def library_page():
        return console_page("library")

    @app.get("/models")
    def models_page():
        return console_page("models")

    @app.get("/persona")
    @app.get("/injections")
    def persona_page():
        return console_page("persona")

    @app.route("/api/persona/<resource>", methods=["GET", "POST"])
    def persona_resource(resource):
        return core_app.manager.dispatch_http("persona", request.method, "/" + resource, request)

    @app.get("/prompts")
    def prompts_page():
        return console_page("prompts")

    @app.get("/assistant")
    def assistant_page():
        return console_page("assistant")

    @app.get("/logs")
    def logs_page():
        return console_page("logs")

    @app.get("/status")
    def status_page():
        return console_page("status")

    @app.get("/status/history")
    def history_page():
        return console_page("history")

    @app.get("/login")
    def login_page():
        destination = gateway.login_destination(request.args.get("return_to"), request.args.get("next"))
        if g.owner_session and g.owner_session["authenticated"]:
            return redirect(destination, code=303)
        if gateway_login is not None:
            content, content_type = gateway_login.page("login", request, login_destination=destination)
            return Response(content, content_type=content_type)
        return console_page("login", login_destination=destination)

    @app.get("/static/<name>")
    def console_static(name):
        assets = gateway_login if gateway_login is not None and name in LOGIN_ASSET_NAMES else core_app.services.get("console.web")
        content, content_type = assets.asset(name)
        return Response(content, content_type=content_type)

    @app.get("/api/platform/status")
    def platform_status():
        return jsonify(core_app.snapshot())

    @app.get("/api/platform/modules")
    def platform_modules():
        return jsonify(items=core_app.manager.list())

    @app.post("/api/platform/modules/<module_id>/<action>")
    def module_action(module_id, action):
        if action not in {"start", "stop", "reload"}:
            raise AuthError("action_unknown", "模块操作不存在。", 404)
        item = getattr(core_app.manager, action)(module_id)
        failed = item.get("state") == "error"
        return jsonify(success=not failed, item=item), 409 if failed else 200

    @app.route("/api/platform/config/<module_id>", methods=["GET", "PUT"])
    def module_config(module_id):
        if request.method == "GET":
            return jsonify(core_app.manager.configuration(module_id))
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {"config"} or not isinstance(body["config"], dict):
            raise AuthError("config_invalid", "配置请求需要一个 config 对象。")
        result = core_app.manager.update_config(module_id, body["config"])
        return jsonify(success=True, item=result)

    @app.get("/api/platform/resources")
    @app.get("/v1/system/status")
    def resources():
        if request.args:
            raise AuthError("invalid_query", "实时状态接口不接受查询参数。")
        provider = core_app.services.get("system.resources")
        if request.method == "HEAD":
            return "", 200
        return jsonify(provider.collect())

    @app.get("/v1/system/history")
    def resource_history():
        if set(request.args) - {"interval"} or len(request.args.getlist("interval")) > 1:
            raise AuthError("invalid_query", "历史统计只接受一个 interval 参数。")
        try:
            interval = int(request.args.get("interval", "60"))
        except ValueError:
            raise AuthError("invalid_interval", "统计粒度应为 60、600 或 1800 秒。") from None
        if interval not in {60, 600, 1800}:
            raise AuthError("invalid_interval", "统计粒度应为 60、600 或 1800 秒。")
        return jsonify(core_app.services.get("system.resources").history(interval))

    @app.get("/api/platform/logs")
    def logs():
        try:
            limit = int(request.args.get("limit", "100"))
        except ValueError:
            raise AuthError("limit_invalid", "日志条数必须是整数。") from None
        if not 1 <= limit <= 500:
            raise AuthError("limit_invalid", "日志条数范围是 1 至 500。")
        return jsonify(items=core_app.logs.list(limit=limit))

    @app.route("/api/modules/<module_id>/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @app.route("/api/modules/<module_id>/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def module_http(module_id, path):
        return core_app.manager.dispatch_http(module_id, request.method, "/" + path, request)

    return app
