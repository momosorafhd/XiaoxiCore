# DSH 会话桥与原生提示覆盖

运行普通私聊所需的是 DSH 会话桥插件。它由用户安装的宿主加载，并使用该宿主的模型、账号、工具循环和持久会话。Core 需要同机回环连接。已测组合为 Linux、Node 24 和 DSH `0.2.2-rc.2`。安装方式、模型与 profile 由使用者决定。[两端加载流程](本机部署.md)

## 新会话怎样取得提示

管理台保存角色和各场景模板。创建日常、整理或独立工作会话时，Core 形成提示快照并交给桥。DSH 插件在这个会话的 agent 上注册专属工具及提示钩子。新原生设置随新会话生效。DSH 宿主报告可用的渲染钩子；只有需要替换相应原生文案时才安装版本补丁。当前仓库提供时间、工作目录、工作区说明、计划模式、模型选择、运行上下文、会话标题和压缩相关的八个运行时文件差异。[差异及校验值](../integrations/native-prompts/manifest.json)

## 检查、应用与回退

需要 Python 和 Git。准备一份独立、可写的 DSH 安装副本，根目录应含 `node_modules`。先停用该副本的 DSH 进程。用以下命令检查上游版本和所有目标文件，输出的每项状态为 `original` 或 `patched`。

```sh
python3 scripts/native_prompts.py check "$DSH_INSTALL"
python3 scripts/native_prompts.py apply "$DSH_INSTALL"
python3 scripts/native_prompts.py check "$DSH_INSTALL"
```

启动使用该安装副本的 DSH 并重新创建助手会话。在原生提示页核对宿主能力。回退时停止这个进程，运行 `python3 scripts/native_prompts.py revert "$DSH_INSTALL"` 再重启。工具会校验版本、原始和目标 SHA-256，在临时目录生成并核对变更，再逐文件原子替换。中断后重新执行同一命令即可。若文件被包管理器改动、已被其他补丁修改，或指向外部符号链接，先取得匹配的独立安装副本。升级到其他版本需重新适配钩子并做请求级验证。

机器认证、网页登录和模型账号分别配置。会话桥用私有 JSON 与 Core 环境中的机器令牌。DSH 自己管理模型账号。需要浏览器跨服务登录时再部署独立的网页插件。[可选单点登录](LAN.md)

## 隔离联调

准备已安装的上述 DSH 版本、Node 24 可执行文件与 Core 虚拟环境。选择两个空闲回环端口，并预留一份 DSH 副本所需的磁盘空间。下例在新的私有目录复制宿主、应用补丁并启动本机合成模型，验证专属会话、原生提示、工具回调及插件卸载重挂。使用结束后可移除这次专属临时目录，其中包含合成数据和临时机器令牌。

```sh
qa_dir=$(mktemp -d -t dsh-fixture-XXXXXXXX)
.venv/bin/python scripts/verify_dsh.py "$qa_dir" \
  --installed-dsh "$DSH_INSTALL" --node "$(command -v node)" \
  --bridge-port 18868 --core-port 18867
```
