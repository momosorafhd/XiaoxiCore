"""Template and static asset service for the owner console."""
from pathlib import Path
from urllib.parse import urlsplit

from ...platform.errors import PlatformError
from jinja2 import Environment, FileSystemLoader, select_autoescape


LOGIN_ASSET_NAMES = frozenset({"login.js", "style.css"})
PAGES = ("overview", "modules", "logs", "status", "history", "login", "library", "prompts", "assistant", "models", "persona")


class ConsoleAssets:
    def __init__(self, *, pages=PAGES, asset_names=None, service_links=()):
        directory = Path(__file__).parent
        types = {"css": "text/css; charset=utf-8", "js": "application/javascript; charset=utf-8"}
        static = directory / "static"
        files = static.iterdir() if asset_names is None else (static / name for name in asset_names)
        self._assets = {file.name: (file.read_bytes(), types[file.suffix[1:]])
                        for file in files if file.suffix[1:] in types}
        self._templates = Environment(loader=FileSystemLoader(directory / "templates"),
                                      autoescape=select_autoescape(["html"]), auto_reload=False)
        self._templates.globals["service_links"] = service_links
        self._pages = {name: self._templates.get_template(name + ".html") for name in pages}

    def asset(self, name):
        if name not in self._assets:
            raise PlatformError("asset_not_found", "管理台资源不存在。", 404)
        return self._assets[name]

    def page(self, name, request, **context):
        if name not in self._pages:
            raise ValueError("管理台页面不存在。")
        # These are real samples, including an explicit unavailable response on
        # Windows. Never select the legacy template's synthetic-demo wording.
        value = self._pages[name].render(request=request, development=False, page=name, **context)
        return value.encode("utf-8"), "text/html; charset=utf-8"


class Module:
    manifest = {"id": "console", "name": "管理台", "version": "0.1.0", "api_version": 1, "requires": []}

    @staticmethod
    def validate_config(config):
        if not isinstance(config, dict) or set(config) - {"serviceLinks"}:
            raise ValueError("管理台配置只接受 serviceLinks（外部服务入口）")
        links = config.get("serviceLinks", [])
        if not isinstance(links, list):
            raise ValueError("serviceLinks 需要入口数组")
        for link in links:
            if (not isinstance(link, dict) or set(link) - {"label", "url", "description"}
                    or not isinstance(link.get("label"), str) or not link["label"].strip()
                    or not isinstance(link.get("description", ""), str)):
                raise ValueError("服务入口需要名称、网址和可选的说明")
            url = link.get("url")
            if not isinstance(url, str) or "\\" in url or any(ch.isspace() or ord(ch) < 32 for ch in url):
                raise ValueError("服务入口需要完整的 HTTP(S) 网址")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                raise ValueError("服务入口需要不含账号密码的 HTTP(S) 网址")
            _ = parsed.port  # Also reject malformed or out-of-range ports.

    def start(self, context):
        self.validate_config(context.config)
        context.services.provide("console.web", ConsoleAssets(service_links=context.config.get("serviceLinks", [])))

    def stop(self):
        pass  # The context owns and removes the registered page service.

    def status(self):
        return {"entry": "/", "pages": ["总览", "助手运行", "图书馆", "模型设置", "角色卡与注入", "原生提示", "模块管理", "资源监控", "历史统计", "运行日志"],
                "frontend": "legacy-console"}
