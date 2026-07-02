# cc-light — Claude Code 桌面悬浮交通灯

一个悬浮在桌面最顶层的小交通灯,实时反映**所有** Claude Code 窗口的复合状态。多窗口按优先级聚合(任一红 > 任一黄 > 任一绿 > 灰),一眼知道「现在最该去哪个窗口」;灯下方一排小点展示每个窗口的状态。

纯 Python(tkinter 灯窗口)+ Node.js(hook),零第三方依赖。

## 状态含义

| 灯色 | 含义 | 触发事件 |
|------|------|---------|
| 🔴 红 | 弹出权限确认框 | `PermissionRequest` |
| 🟡 黄 | 运行中(呼吸) | `UserPromptSubmit` / `PostToolUse`(决策类工具完成) |
| 🟢 绿 | 完成 / 空闲 / 就绪 | `SessionStart` / `Stop` / `Notification(idle_prompt)` |
| ⚫ 全暗 | 无活跃会话 | — |

> 用户中断不触发 `Stop`(CC 官方设计),靠 `Notification(idle_prompt)` 兜底变绿,再加超时降级双保险。

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

# 2. 安装 hooks(自动备份 settings.json)
python install-hooks.py install

# 3. 查看注入情况
python install-hooks.py status
```

⚠️ **hooks 改动后需重启 Claude Code 才生效**(本会话已加载旧配置)。

## 鼠标操作

- **悬停** → 显示会话明细(移开 300ms 自动关闭,明细上可停留查看)
- **左键拖动** → 移动位置(松开记忆)
- **双击** → 切换是否置顶
- **右键** → 菜单:清理不活跃会话 / 超时兜底开关 / 超时时间 / 手动切色 / 复位位置 / 重启 / 退出

## 配置(右键菜单,存 `config.json`)

- **超时兜底**:开关。开启时 yellow 超过阈值自动算 green(兜底 Stop 丢失 / 用户中断不触发 Stop)。
- **超时时间**:60 / 120 / 180 / 300 / 600 秒单选。

## 文件清单

| 文件 | 作用 |
|------|------|
| `cc_light.py` | 灯窗口(GUI,小尺寸圆角悬浮)+ CLI;扫描 sessions 聚合 |
| `hook.js` | hook 处理器(node,读 stdin 写 session)。**用 node 不用 pythonw**(pythonw 在 Windows 接 stdin 偶发失败) |
| `install-hooks.py` | 幂等注入 / 卸载 hook |
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

## 平台

Windows 11 + Python 3.8 + Node.js。圆角悬浮用 `-transparentcolor`,复位用 Win32 workarea API + DPI aware。
