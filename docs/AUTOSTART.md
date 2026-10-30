# Windows 持久自动启动

本项目采用 **B：当前 Windows 用户登录时由任务计划程序启动**，并处理 **C：进程存在但窗口隐藏**。配置保存在 Windows 当前用户计划任务中，与 Codex 的聊天、项目、账号退出无关。

## 检查结论与 A / B / C

| 情况 | 本项目的处理 |
| --- | --- |
| A：打开 Codex 或登录 Codex 后运行项目 | 未找到有公开文档支持的桌面登录回调或项目自启动接口。后台进程通过 Windows 窗口事件感知客户端出现，恢复侧栏；它不判断账号是否登录成功。 |
| B：登录 Windows 后运行项目 | 当前用户的交互登录任务，延迟约 10 秒，使用绝对 Python / 项目路径与工作目录。独立于 Codex 终端和项目运行按钮。 |
| C：窗口关闭、隐藏或最小化 | 自动启动开启时 × / Esc 隐藏到后台；托盘、`--restore`、普通启动入口均可恢复。Codex 最小化时跟随窗口一起隐藏，客户端恢复时重新显示。 |

实际项目为 Python / Tkinter，入口是 `run.py`。可见运行实例的命令为 `pythonw.exe <项目目录>\run.py`，启动器 `Start-Widget.ps1` 与 `启动面板.vbs` 复用该入口。仓库中没有找到定义 Codex 运行按钮的本地环境配置，因此不能把观察到的进程命令当作已读取到的按钮配置。

原有的 `HKCU Run` 只在 Windows 登录时执行一次；它不能因 Codex 重开而重启已退出的项目。原有 × / Esc 会结束进程，重复启动也只退出，没有唤回窗口。现在使用独立后台守护、关闭时隐藏和跨进程恢复信号解决这些情况。

Codex 官方的 [本地环境文档](https://learn.chatgpt.com/docs/environments/local-environment) 描述了手动执行的 Actions 和新建 worktree 时执行的 setup script；未提供桌面登录后项目自启动的受支持接口。本项目不修改 Codex 程序、内部数据库或全局状态文件。

## 开启、关闭、检查

在项目目录打开 PowerShell；若 `python` 不可用，可改为 `py` 或本机 Python 完整路径。

```powershell
# 安装/更新当前用户任务，并立即运行
python run.py --enable-startup

# 检查任务、进程、窗口状态及日志路径
python run.py --startup-status

# 恢复窗口；没有进程时由计划任务启动
python run.py --restore

# 明确结束本次运行，保留下次登录启动配置
python run.py --stop

# 删除本项目自动启动任务；保留当前运行的界面
python run.py --disable-startup
```

外观设置中的「Windows 登录后自动运行（当前用户）」与上述命令使用相同实现。明确退出后需要托盘以外的入口恢复，因为程序退出时也会删除托盘图标；双击 `启动面板.vbs` 或 `--restore` 即可。启用状态下关闭窗口只是隐藏，托盘仍可用。

正常状态包含 `enabled: true`、`valid: true`、`instance_running: true`、`supervisor_running: true`。`runtime.managed: true` 表示窗口由后台管理。`runtime.state` 的常见取值：

| 状态 | 含义 |
| --- | --- |
| `sidebar` / `following` | 已显示缩略条或跟随浮窗 |
| `waiting_for_codex` | 程序存在，等待客户端窗口 |
| `client_minimized` | 客户端最小化或隐藏 |
| `hidden_by_user` | × / Esc 隐藏，可恢复 |
| `independent` | 独立显示；也可能是无客户端时显式恢复的临时浮窗 |
| `stopped` | 已明确退出，最后状态文件保留作诊断 |

`instance_running` / `supervisor_running` 由 Windows 命名互斥量实时检查；不要把旧状态文件里的 PID 当作进程一定存活。计划任务运行中可能显示 `last_result: 267009`（`0x41301`），这是“正在运行”，不是启动错误。

## 持久性与恢复

任务名由当前用户及项目绝对路径计算：`CodexQuotaWidget-<16 位摘要>`。使用 `InteractiveToken` 与最低运行级别，只在此用户登录桌面时启动，不保存 Windows 密码，不使用“以最高权限运行”。参考 Microsoft 的 [任务安全上下文](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks)。通常无需管理员权限；若组织策略禁止当前用户注册任务，会报告失败，不自动请求提权。

系统任务执行 `pythonw.exe "<项目目录>\run.py" --background`，守护进程再运行同一入口的内部 `--worker` 模式。守护进程阻塞等待界面子进程或自己的控制事件，没有账号轮询；界面异常退出或启动失败时按 1、5、15、30、60 秒退避，以后最长间隔 60 秒。稳定运行五分钟后重置退避。用户明确选择「退出程序」或 `--stop` 会正常结束，避免与用户关闭意图冲突。

计划任务还配置每分钟失败重试、最多 10 次，作为守护进程自身失败时的补充。界面进程的恢复由项目自己的守护实现并经过实际验证。任务使用 [IgnoreNew 多实例策略](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-multipleinstancespolicy-settingstype-element)，界面和守护各有当前会话的命名互斥量；再次启动会向已有窗口发送恢复事件。不同的 `--config` 和演示配置仍可独立运行，用于测试。

后台监听 Windows `WinEvent` 窗口事件并缓存客户端句柄；30 秒低频扫描用于事件丢失时恢复。原窗口跟随节奏复用缓存，不反复枚举所有窗口，不查询账号状态。正常额度刷新频率仍由原设置决定。

默认侧栏模式下，关闭 Codex 后隐藏窗口并暂停无客户端时的定时额度读取；后台继续存在，下一次出现客户端时恢复并刷新。选择「独立悬浮」可在关闭 Codex 后继续显示。无客户端、客户端最小化或隐藏时，显式恢复窗口会临时显示独立面板，客户端可见后回到原模式，不覆盖保存的侧栏设置。

Windows 重启后需要登录该用户桌面；不会在尚未登录 Windows 的锁屏阶段显示界面。任务与退出 Codex 账号无关。关闭自动启动不会删除背景或窗口配置。移动项目或更换 Python 前，先关闭自动启动，迁移后重新开启，防止旧绝对路径失效。

## 认证与启动时机

额度读取实现保持原样，使用 Codex App Server 的 [`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server) 及原有历史快照兜底。窗口和守护不要求 Codex 桌面端持续运行，但实时额度需要可用的 Codex CLI、网络及其管理的账户登录状态。

登录 Windows 时认证暂不可用不会使守护退出：界面按原逻辑显示错误或带来源、更新时间的历史快照，在下一个既有刷新周期重试，也可点 ↻。退出并重新登录 Codex 不删除本项目任务；若登录窗口复用相同句柄，登录成功后的额度最迟在下一次配置的刷新周期更新，不依赖未公开的登录事件。CLI 与桌面端应使用相同账号和 `CODEX_HOME`。

启用时仅保存可选的非敏感 `CODEX_HOME` 路径到 `.local/startup-context.json`，后台没有显式环境变量时使用它。不复制 API Key、账号令牌、全部环境变量或 Codex 登录文件；不会自行替用户完成认证。

## 日志及验收

界面日志 `.local/runtime.log`；守护日志 `.local/settings.supervisor.runtime.log`。每份日志限制为 256 KiB 并保留两份备份；只记录固定操作事件、进程状态、异常类型及代码栈位置，不记录原始异常文本、账户返回值或凭据。对应 `runtime.json` 仅在状态变化时写入，避免不断刷盘。

本次验收（2026-10-30）：98 项单元测试通过；临时当前用户任务注册、运行、删除与带空格路径验证通过；临时子进程退出码 7 后约 1.04 秒恢复；正式界面进程异常退出后约 1.72 秒恢复；6 次并发普通启动和 2 次计划任务请求保持一个界面；真实 Tk 窗口、原生托盘消息恢复、客户端缺失/返回模拟、客户端最小化时恢复浮窗再回侧栏的模拟、明确停止后恢复均通过。实际额度接口也成功返回五小时与一周数据，当前用户的 30 秒刷新设置保持不变。临时测试任务已删除。

以下验收会中断当前桌面会话或账号，需用户手动完成：

1. 完全退出 Codex 后再打开，不进入项目；左侧缩略条应自行出现，悬停展开。
2. 退出 Codex 账号后再登录，确认任务仍启用，额度在下一刷新周期恢复；历史快照应继续明确显示来源。
3. 重启 Windows 并登录同一用户，等待约 10 秒；打开 Codex 后检查缩略条。也可先看系统托盘，或运行 `--startup-status` 检查后台。
4. 断网或 CLI 临时不可用后恢复，确认错误不结束进程，恢复后按原刷新周期重读。

验证时可多次点击原启动入口，预期始终只有一个额度窗口及一个后台守护进程；这两个进程不是重复窗口。
