"""A separate module used to demonstrate reloading the whole plugin package."""

LABEL = "示例插件"


def greeting(text):
    return f"{LABEL}：{text}"
