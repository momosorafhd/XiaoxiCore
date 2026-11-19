"""Owner-editable persona settings shared by the assistant and the DSH bridge."""
from functools import partial

from xixi_core.modules.persona import PersonaStore, persona_http


class Plugin:
    @staticmethod
    def validate_config(config):
        if config != {}:
            raise ValueError("请在角色卡与注入页面编辑设置。")

    def start(self, context):
        self.validate_config(context.config)
        persona = PersonaStore(context.data_dir / "persona.sqlite3")
        context.add_cleanup(persona.close)
        context.services.provide("persona", persona)
        for resource in ("injections", "environment", "summaries"):
            for method in ("GET", "POST"):
                context.http.register(method, "/" + resource, partial(persona_http, persona, resource))

    def status(self):
        return {"entry": "/persona"}

    def stop(self):
        pass
