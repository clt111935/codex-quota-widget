# Codex 用量伴侣

Windows 桌面小工具，为 Codex 桌面客户端提供一个可跟随窗口的额度面板。

- 显示 **5 小时 / 一周剩余百分比**与重置倒计时，每 60 秒刷新，支持手动刷新。
- 三套配色：月夜、深海、松林；支持本地 PNG / JPG / WebP / BMP 背景、自定义背景遮罩和窗口透明度。
- 跟随 Codex 窗口移动及最小化，也可拖到任意位置独立置顶；支持精简模式。
- 只读取额度，不发起对话、不消耗推理额度，也不修改 Codex 安装文件。

这是一个独立的 Windows 悬浮窗口，覆盖在 Codex 窗口上方；它不是对 Codex 客户端原生界面的修改。

## 启动

需要 **Windows 10 / 11、Python 3.11 或更新版本**，以及已登录 ChatGPT 账户的 Codex 桌面客户端或 Codex CLI。

在项目目录打开 PowerShell：

```powershell
python -m pip install -r requirements.txt
python run.py
```

也可以直接双击 **`启动面板.vbs`**，隐藏终端启动。右键 `Start-Widget.ps1` →「使用 PowerShell 运行」也可以。启动器仅在本次进程中使用执行策略参数，不修改系统执行策略。

不使用图片时无需安装 Pillow，文字和进度条仍可使用。若 `python` 命令不可用，使用 Python 的完整路径，或改用 `py`。

```powershell
py run.py
```

## 操作

| 操作 | 效果 |
| --- | --- |
| 右上角 ↻ | 立即刷新真实账户额度 |
| 齿轮 / 右下角「外观设置」 | 主题、背景图片、遮罩、透明度、精简模式、刷新频率 |
| 左下角「跟随 Codex / 独立悬浮」 | 切换窗口模式 |
| 完整模式底部的数据来源区域 | 查看采集时间、重置时间，或手动选择 Codex CLI |
| 拖动顶部标题区域 | 自由摆放，自动切换为独立悬浮 |
| 右键面板 | 快捷菜单 |
| × / Esc | 退出面板 |

跟随模式识别 Codex 桌面程序，包括安装包名称为 `OpenAI.Codex`、进程显示为 `ChatGPT.exe` 的版本。关闭或找不到 Codex 时，面板会保留为可操作的独立窗口。精简模式下点击底部状态区打开设置。

配置保存在项目下的 `.local/settings.json`。演示模式使用单独的 `.local/demo-settings.json`，不会覆盖正常设置。删除相应配置文件即可恢复默认设置。背景图片保存的是本地文件路径，移动图片后需要重新选择。

## 数据来源和显示边界

优先调用官方文档提供的 Codex App Server `account/rateLimits/read` 方法，复用 Codex 自己管理的登录状态。流程仅为 `initialize` → `initialized` → `account/rateLimits/read`；不会调用 `thread/start`、`turn/start` 或任何额度重置方法。

参考：[OpenAI 官方 App Server 文档](https://learn.chatgpt.com/docs/app-server)。

如果实时接口不可用，会尝试从本机 `$CODEX_HOME/sessions`（默认 `~/.codex/sessions`）近期会话日志的额度事件中读取历史快照，并在面板标出来源和更新时间。程序只提取额度字段，不展示、保存或上传对话内容，不直接读取 `auth.json`，没有自建服务器或分析上报。Codex App Server 在查询实时额度时会连接 OpenAI 服务。

- 剩余百分比 = `100 - usedPercent`，不是剩余条数或 Token 数。
- 按接口实际返回的 300 分钟和 10080 分钟窗口归类，不把其他窗口当成 5 小时或一周。
- 某些账户没有相应周期限制，或服务没有返回该窗口，面板会显示「未提供」。不将缺失数据解释为 100%。
- 历史数据到达重置时间后显示「等待刷新 / 上次剩余」，不会自行把百分比重置为 100%。
- 账户规则可能变化，以实际服务返回值为准。[官方用量说明](https://learn.chatgpt.com/docs/pricing)
- CLI 与桌面客户端应使用相同账户及 `CODEX_HOME`。若手动切换过 CLI 账户，请核对两者的登录状态；历史日志可能来自切换前的账户。

## 诊断和开发

```powershell
# 只读取并输出真实额度 JSON，不启动界面
python run.py --check

# 演示界面：不读取账户、不调用 Codex
python run.py --demo

# 生成演示截图并退出（需要 Pillow）
python run.py --demo --screenshot docs/preview.png

# 单元测试
python -m unittest discover -s tests -v
```

如果实时读取失败，先检查 Codex 是否已登录、`codex --version` 是否正常。使用 API Key 的账户不一定提供订阅周期额度。CLI 不在 PATH 时，读取器会尝试查找桌面版附带的 CLI；也可以右键面板 →「数据与连接」→「选择 codex.exe」手动指定路径，或在 Python 中使用 `UsageService(codex_command=...)`。启动失败的诊断保存在 `.local/startup-error.log`。

## 界面预览

下图是演示数据，真实运行时会读取你的账户额度。

![Codex 用量伴侣](docs/preview.png)

## 文件

```text
run.py                     启动、演示、诊断入口
quota_widget/app.py        面板和外观设置
quota_widget/usage.py      额度读取与解析
quota_widget/windows.py    Windows 窗口跟随
quota_widget/settings.py   设置校验与保存
tests/                     数据解析、RPC、配置测试
启动面板.vbs                双击启动入口
Start-Widget.ps1           隐藏终端的启动脚本
requirements.txt           可选的图片支持依赖
```
