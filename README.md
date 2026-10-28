# cc-light — Claude Code / Codex / DeepSeek Harness 桌面悬浮交通灯

一个悬浮在桌面最顶层的小交通灯,实时反映 Claude Code、Codex 和 DeepSeek Harness 会话的复合状态。多窗口按优先级聚合(任一红 > 任一黄 > 任一绿 > 灰),一眼知道「现在最该去哪个窗口」;灯下方一排小点展示每个窗口的状态。

Codex 的运行、完成和提问状态无需安装 hook:灯窗口每 3 秒读取 `~/.codex/sessions` 的回合事件,通过 `thread-writer-locks` 文件的实际持有进程识别活跃会话,支持继续旧会话和共享后台服务。`task_started` 显示黄灯,`task_complete` / `turn_aborted` 显示绿灯;会话锁释放后从灯中移除,历史遗留锁文件不会被当成活跃会话。右键删除后保持隐藏,只有再次发送用户消息才恢复显示;后台开始新回合、工具回调、完成通知或重启不会恢复;恢复后完成的会话保留绿灯。隐藏状态跨交通灯重启保留,旧版隐藏记录升级后仍保持隐藏,直到再次发送用户消息。旧版 Codex 没有会话锁时按终端进程启动时间匹配。明细中以 `Codex · 项目名` 区分来源。桌面版的 `request_user_input` / `request_user_input_async` 提问显示红灯,异步提问的 `{accepted:true}` 不算回答;用户回复后恢复黄灯,完成/中断后变绿。普通回复里的问句不做文本猜测。

Codex 权限确认通过只记录状态的 `PermissionRequest` / `PostToolUse` hook 接入,并只读查询本地 `logs_2.sqlite` 中实际的审批提交,确认后无需等长命令结束就能恢复黄灯。多个权限请求全部处理后才退出红灯。运行 `python install-codex-hooks.py install`,然后在 Codex CLI 的 `/hooks` 中审阅并信任两条 `cc-light Codex indicator` hook;未信任前权限红灯尚不生效。其他 hook 保留并自动备份,不修改信任配置或审批决策。运行 `python install-codex-hooks.py uninstall` 可移除。安装后在新回合验证,如桌面版未重新加载配置则重启桌面版。

DeepSeek Harness 同样无需 hook:灯窗口每 2 秒读取桌面版实时写入的投影缓存 `~/.dsh/storages/session_projcache/sessions/<id>.json`,turn/step 未结束显示黄灯,空闲显示绿灯,`ask_user_question` 等待回答显示红灯。只展示本次应用启动后动过的会话,应用退出后自动消失;空闲会话 1 小时无更新后隐去,运行中的会话不受此限。明细中以 `DeepSeek · 项目名` 区分来源。权限审批只推送到渲染进程(落盘的 `approval/asked` 在压缩会话日志里),因此等待审批时仍显示黄灯,不触发红灯。点击聚焦 DeepSeek Harness 窗口;它是单实例桌面应用,无法从外部切换具体会话 tab。

纯 Python(tkinter 灯窗口)+ Node.js(hook),零第三方依赖。

点击会话:终端里运行的会话跳到对应终端窗口(Trae 走扩展精确定位 tab)。ProjectManager 管理页 spawn 的后台无头任务没有终端窗口,点击不再自动打开浏览器,改在灯窗口旁提示管理页地址,由用户自行在浏览器查看;焦点文件仍会写,已打开的管理页会自动切到该项目。Claude Code daemon 恢复的后台会话(`--bg-pty-host` + `--session-id ... --fork-session --resume`)同样没有终端窗口,明细里标为 `项目名 ·后台`,点击提示在 Claude Code 后台运行,不会误跳到同名终端窗口。

## 状态含义

| 灯色 | 含义 | 触发事件 |
|------|------|---------|
| 🔴 红 | 弹出权限确认框 | `PermissionRequest` |
| 🟡 黄 | 运行中(呼吸) | `UserPromptSubmit` / `PostToolUse`(决策类工具完成) |
| 🟢 绿 | 完成 / 空闲 / 就绪 | `SessionStart` / `Stop` / `Notification(idle_prompt)` |
| ⚫ 全暗 | 无活跃会话 | — |

> 用户中断不触发 `Stop`(CC 官方设计),靠 `Notification(idle_prompt)` 兜底变绿,再加超时降级双保险。
>
> 表中的「触发事件」只适用于 Claude Code。Codex 按 `task_started` / `task_complete` 等回合事件判色;DeepSeek Harness 按投影缓存里的 `turnBoundary` / `sessionStats.openStep` 判黄、`userQuestions.active` 判红。

删除规则适用于 Claude Code、Codex 和 DeepSeek Harness:右键删除后持续隐藏,再次向该会话发消息才恢复;交通灯重启和后台状态变化不会恢复。

Codex 的内部 `Guardian review` 审批复核会话不显示在列表中:按 `thread_source=guardian_review` 或 `source.subagent.other=guardian` 过滤,系统自动发送的复核消息不会使其重新出现;同名的普通聊天不受影响。

Codex 子代理折叠到所属主会话,不再各自显示同名项目条目。子代理运行或等待确认时,其状态按红灯优先于黄灯的规则计入主会话;删除主会话后,子代理活动不会将它恢复。同项目的独立用户会话仍分别显示。

## 多会话聚合

每个 CC 窗口一个 session 文件(`sessions/<session_id>.json`),灯窗口 500ms 轮询、按优先级聚合:

- 任一窗口等确认 → 🔴
- 否则任一在跑 → 🟡
- 否则全完成 → 🟢
- 无会话 → ⚫

灯下方一排小点,每个窗口一个,颜色=该窗口有效状态(点数多时自动缩小)。

## 快速开始

```bash
cd ~/.claude/cc-light

# 1. 启动灯窗口(或双击 start-light.vbs)
pythonw cc_light.py

# 2. 安装 hooks(注入 settings.json;灯窗口自愈防坚果云覆盖)
python install-hooks.py install

# 3. 查看注入情况
python install-hooks.py status
```

⚠️ **新开窗口立即生效**;老窗口需重启才会加载新配置。改 hook 后**也要重启交通灯**(右键→重启),灯窗口才用新代码并开启自愈。运行数据(sessions/config/pos)在 `%APPDATA%\cc-light\`,不再在 `~/.claude` 下。

启动时会检查保存位置是否仍在已连接显示器上,屏幕断开或分辨率变化后自动移回可见区域。Windows 下重复启动只保留一个交通灯实例。

## 鼠标操作

- **悬停** → 显示会话明细(移开 300ms 自动关闭,明细上可停留查看)
- **左键拖动** → 移动位置(松开记忆)
- **右键** → 菜单:清理不活跃会话 / 检测所有会话 / 错误日志 / 快捷记录 / 快捷指令 / **其他设置**(超时兜底、超时时间、监测范围) / 窗口置顶 / 复位位置 / 修正终端窗口 / 重启 / 退出

右键菜单的「仅保留工作中会话」执行一次清理:保留黄灯运行中、红灯等待回答或确认的会话,隐藏绿灯完成/空闲及其他非工作状态的会话。隐藏规则与右键删除一致,再次发消息会恢复,重启不会恢复。这是一次性操作,不会持续过滤之后完成的会话。

## 配置(右键 → 其他设置,存 `config.json`)

- **超时兜底**:开关。开启时 yellow 超过阈值自动算 green(兜底 Stop 丢失 / 用户中断不触发 Stop)。
- **超时时间**:60 / 120 / 180 / 300 / 600 秒单选。
- **监测范围**:Claude Code / Codex / DeepSeek Harness 分别勾选。取消勾选某类后,它的会话不再计入灯色和圆点(比如不想被 Codex 会话干扰时关掉 Codex);重新勾选即恢复。

## 文件清单

| 文件 | 作用 |
|------|------|
| `cc_light.py` | 灯窗口(GUI,小尺寸圆角悬浮)+ CLI;扫描 sessions 聚合 |
| `hook.js` | hook 处理器(node,读 stdin 写 session)。**用 node 不用 pythonw**(pythonw 在 Windows 接 stdin 偶发失败) |
| `install-hooks.py` | 幂等注入 / 卸载 hook(装 settings.json,清理历史 managed 残留) |
| `start-light.vbs` | 静默启动器(**纯 ASCII**,见下方坑) |

## 开机自启(可选)

`Win+R` → `shell:startup` → 把 `start-light.vbs` 创建快捷方式进去。

## 卸载

```bash
python install-hooks.py uninstall   # 移除 hooks(自动备份)
# 右键灯 → 退出
```

## 设计要点(踩过的坑)

- **per-session 文件**:并发安全,各窗口各写。
- **node hook**:读 stdin 稳定(避开 pythonw 偶发失败)。
- **超时降级 + idle_prompt**:双保险,防「中断 / Stop 丢失」卡黄。
- **过期清理**:session 文件 1h 无更新删除。
- **红用 `PermissionRequest` 不用 `Notification`**(`Notification` 含 `idle_prompt` 会误亮红;`idle_prompt` 单独用于 → 绿)。
- **决策后回黄**:`PostToolUse` 只匹配 `AskUserQuestion`/`ExitPlanMode`,决策完成即刷黄(否则会卡红直到 Stop);日常工具不触发、零拖慢。
- **状态识别非 100%**:CC 不给「中断」事件、长思考无事件与中断不可区分,故保留超时兜底(右键可调/可关)。
- **多层执行(子 agent)**:挂 `SubagentStart`/`SubagentStop`,每个 session 维护 `subs` 计数;子 agent 跑期间主 `Stop`/`idle_prompt` 不写 green(保持 yellow),避免「子还在跑却绿灯」。`hook.js` 从只写改为先读后写(合并 `subs`)。
- **`start-light.vbs` 必须纯 ASCII**:VBScript 在中文系统按 GBK 解析,UTF-8 中文注释会报「缺少对象」。
- **daemon 后台会话不能算进「项目进程数」**:CC daemon 恢复后台会话时是三层进程(`daemon run` → `--bg-pty-host` → `--session-id ... --fork-session --resume <旧jsonl>`),`--resume` 会刷新源 jsonl 的 mtime 但它没有自己的进程。若把包装进程算进 `scan_active_jsonl` 的 N,重启时就会补出一个与终端会话同名、termpid 借用别人终端、点击跳错窗口的幽灵会话。现在按命令行区分 `daemon`/`ptyhost`/`session`,排除 `--resume` 源和「进程启动前写入」的 jsonl;后台会话在明细标 `·后台`,点击提示而非跳转。
- **抗坚果云覆盖 + 清空**:`~/.claude` 整个被坚果云双向同步 —— `settings.json` 的 hook 会被「另一台机器的配置」整体冲掉(表现为「同项目多窗口只亮一个灯」),sessions 目录也曾被同步清空。对策:① hook 装用户级 `settings.json`,由灯窗口 `ensure_hooks` 每 20s 自愈重注入;② 运行数据(sessions/config/pos)移到 `%APPDATA%\cc-light\`,不在同步范围。曾试过 managed-settings.json,但 Windows 下在 `C:\Program Files\ClaudeCode\` 需管理员才能写,弃用。

## 平台

Windows 11 + Python 3.8 + Node.js。圆角悬浮用 `-transparentcolor`,复位用 Win32 workarea API + DPI aware。
