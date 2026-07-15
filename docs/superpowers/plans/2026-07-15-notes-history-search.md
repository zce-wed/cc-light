# 快捷记录历史搜索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给快捷记录历史窗口加全文关键词搜索 + 命中高亮。

**Architecture:** 抽两个**纯函数** `match_record` / `preview_snippet`(做匹配与片段截取,可单测,不依赖 tkinter);`show_history` 加搜索框 `Entry`,实时 `pack/pack_forget` 筛选,预览从 `Label` 换只读 `Text` 做 tag 高亮。数据结构(`notes_history.json`)不变。

**Tech Stack:** Python 3 + tkinter(纯 ctypes 项目,无第三方依赖);无测试框架 —— 纯函数用 `assert` 脚本(`python tests/...` 直跑,不依赖 pytest)。

## Global Constraints

- 单文件 `cc_light.py`(tkinter GUI),**不引入任何第三方依赖**。
- `notes_history.json` 数据结构**不变**(仍是 `{ts, text}` 数组、末尾最新)。
- 测试不依赖 pytest:`tests/test_notes_search.py` 用 `assert`,直接 `python tests/test_notes_search.py` 运行。
- `cc_light.py` 顶层 import 安全(只有 `import sys/os/json/time/math` + 常量 + 函数;`run_gui` 在 `__main__` 里),测试脚本 `import cc_light` 不会启动 GUI。
- 提交信息中文,结尾 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- GUI 部分(tkinter)无法自动化测,用手动验证步骤;仅纯函数走 TDD。

---

## File Structure

- **Create** `tests/test_notes_search.py` — 纯函数单测(`assert` 风格,`python` 直跑)。
- **Modify** `cc_light.py`:
  - 模块级新增 `match_record(text, query)`、`preview_snippet(text, query, head=14, ctx=8)`(放 `save_history` 之后,notes 相关函数聚在一起)。
  - `run_gui` 内 `show_history` 改造:标题动态、加搜索框、预览换 `Text`、筛选+高亮、placeholder/Esc、边界。

---

## Task 1: 纯函数 `match_record` + `preview_snippet`(TDD)

**Files:**
- Create: `tests/test_notes_search.py`
- Modify: `cc_light.py`(在 `save_history` 函数之后插入两个模块级函数)

**Interfaces:**
- Produces: `match_record(text:str, query:str) -> bool`;`preview_snippet(text:str, query:str, head=14, ctx=8) -> tuple[str, list[tuple[int,int]]]` —— 返回 `(snippet, spans)`,spans 是相对 snippet 的命中字符区间(用于 Text 高亮)。

- [ ] **Step 1: 写失败测试 `tests/test_notes_search.py`**

```python
# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cc_light as L


def test_match_record():
    # 空 query 恒匹配
    assert L.match_record("任意", "") is True
    # 不区分大小写子串
    assert L.match_record("Hello World", "world") is True
    assert L.match_record("Hello World", "WORLD") is True
    # 子串
    assert L.match_record("abcdef", "bcd") is True
    # query 比文本长 → 不匹配
    assert L.match_record("abc", "abcd") is False
    # 无命中
    assert L.match_record("abc", "xyz") is False
    # 多行/空白被压平
    assert L.match_record("a  b\tc", "a b") is True


def test_preview_snippet_empty_query():
    # 空 query:前 head 字,超长补 …
    s, spans = L.preview_snippet("0123456789ABCDEFGHIJ", "")   # 20 字
    assert s == "0123456789ABCD …"
    assert spans == []
    # 不超长:无 …
    s, spans = L.preview_snippet("短文本", "")
    assert s == "短文本"
    assert spans == []


def test_preview_snippet_hit_middle():
    s, spans = L.preview_snippet("今天要改 cc-light 的搜索功能很好用", "搜索")
    assert "搜索" in s
    assert len(spans) == 1
    a, b = spans[0]
    assert s[a:b] == "搜索", (s, spans)
    assert s.startswith("…")   # 命中不在开头 → 头部补 …


def test_preview_snippet_hit_start():
    s, spans = L.preview_snippet("搜索功能很好用今天要改", "搜索")
    a, b = spans[0]
    assert s[a:b] == "搜索"
    assert not s.startswith("…")   # 命中在开头 → 不补 …


def test_preview_snippet_no_hit():
    s, spans = L.preview_snippet("abc", "xyz")
    assert s is None
    assert spans == []


if __name__ == "__main__":
    test_match_record()
    test_preview_snippet_empty_query()
    test_preview_snippet_hit_middle()
    test_preview_snippet_hit_start()
    test_preview_snippet_no_hit()
    print("ALL PASS")
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `python tests/test_notes_search.py`
Expected: `AttributeError: module 'cc_light' has no attribute 'match_record'`

- [ ] **Step 3: 在 `cc_light.py` 的 `save_history` 之后插入两个函数**

```python
def match_record(text, query):
    """记录 text 是否匹配搜索 query(不区分大小写子串;空 query 恒匹配)。"""
    if not query:
        return True
    return query.lower() in " ".join(text.split()).lower()


def preview_snippet(text, query, head=14, ctx=8):
    """生成历史预览片段 + 命中区间(相对片段,用于 Text 高亮)。
    空 query:返回压平后前 head 字(超长补 ' …'),空区间。
    有 query 且命中:返回命中词前后各 ctx 字的片段(头尾按需补 '…'),1 个区间。
    未命中:返回 (None, []) —— 调用方应先用 match_record 过滤。"""
    flat = " ".join(text.split())
    if not query:
        snippet = flat[:head] + (" …" if len(flat) > head else "")
        return snippet, []
    fl = flat.lower()
    idx = fl.find(query.lower())
    if idx < 0:
        return None, []
    start = max(0, idx - ctx)
    end = min(len(flat), idx + len(query) + ctx)
    span_off = 1 if start > 0 else 0       # 头部 '…' 占 1 字符,命中区间随之偏移
    snippet = ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")
    span_start = (idx - start) + span_off
    return snippet, [(span_start, span_start + len(query))]
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `python tests/test_notes_search.py`
Expected: `ALL PASS`

- [ ] **Step 5: 提交**

```bash
git add tests/test_notes_search.py cc_light.py
git commit -m "feat: 快捷记录搜索纯函数 match_record/preview_snippet + 单测" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: 搜索框 + 标题计数 + 实时筛选(预览暂留 Label)

**Goal:** 历史窗口加搜索框,输入实时 `pack/pack_forget` 筛选 + 标题显示「匹配 X / 共 Y」。本任务预览仍是原 `Label`(前 14 字),高亮在 Task 3。

**Files:**
- Modify: `cc_light.py` 的 `show_history`(标题区 + 列表创建 + 新增 `_apply_filter`)

**Interfaces:**
- Consumes: `match_record`(Task 1)。
- Produces: `show_history` 内的 `_apply_filter(query)` 闭包、`rows` 列表 `[(it, row_frame, preview_widget), ...]`。

- [ ] **Step 1: 改标题行 —— 把固定文本改成可更新变量**

定位 `show_history` 里原来的标题 `Label`:

```python
        tk.Label(win, text=" 快捷记录历史(%d 条)" % len(items), fg="#9a9a9a", bg=BG,
                 font=("Microsoft YaHei", 9)).pack(side="top", fill="x", padx=6, pady=(6, 4))
```

替换为(存引用 + 标题行用 Frame 容纳标题和搜索框):

```python
        top = tk.Frame(win, bg=BG)
        top.pack(side="top", fill="x", padx=6, pady=(6, 4))
        title_lbl = tk.Label(top, text=" 快捷记录历史(共 %d 条)" % len(items),
                             fg="#9a9a9a", bg=BG, font=("Microsoft YaHei", 9))
        title_lbl.pack(side="top", anchor="w")
```

- [ ] **Step 2: 收集 rows —— 把逐条创建的 row 记进列表**

定位原来 `for it in items:` 循环里创建 `row` / `t_lbl` 的位置。在循环**之前**加 `rows = []`,在循环内 `row` 创建后、绑定 `_click`/`_right` 之后,追加:

```python
                rows.append((it, row, t_lbl))
```

(此时 `t_lbl` 仍是 `Label`,Task 3 会换成 `Text`;元组第三项先叫 `t_lbl`,Task 3 统一改名 `ptxt`。)

- [ ] **Step 3: 加搜索框 `Entry` + 占位 + 绑定筛选(无高亮版)**

在标题 `top` 之后、`body` 之前插入搜索框区(空历史 `items` 为空时跳过,Task 4 处理;本任务先无条件加,`_apply_filter` 对空列表天然安全):

```python
        search_var = tk.StringVar()
        entry = tk.Entry(win, textvariable=search_var, bg="#2a2a2a", fg="#e8e8e8",
                         insertbackground="#e8e8e8", relief="flat", bd=0,
                         font=("Microsoft YaHei", 9))
        entry.insert(0, "搜索…")
        entry.config(fg="#777777")
        entry.pack(side="top", fill="x", padx=6, pady=(0, 4))
```

- [ ] **Step 4: 写 `_apply_filter`(无高亮版)**

在 `rows` 收集之后、`win` 事件绑定之前插入:

```python
        def _apply_filter(query):
            q = (query or "").strip()
            matched = 0
            for it, row, pw in rows:
                if L_match_record(it.get("text", ""), q):
                    if not row.winfo_ismapped():
                        row.pack(side="top", fill="x", padx=2, pady=2)
                    matched += 1
                else:
                    if row.winfo_ismapped():
                        row.pack_forget()
            if q:
                title_lbl.config(text=" 快捷记录历史 · 匹配 %d / 共 %d 条" % (matched, len(rows)))
            else:
                title_lbl.config(text=" 快捷记录历史(共 %d 条)" % len(rows))
            _sync_sb(cv.winfo_height())
```

> 注意:模块内调用纯函数用全局名。因为 `show_history` 在 `run_gui` 内、`match_record` 在模块级,直接写 `match_record(...)` 即可(下面 Step 5 修正)。

- [ ] **Step 5: 修正调用名 + 绑定 KeyRelease**

把 Step 4 里的 `L_match_record` 改回 `match_record`(模块级函数,`run_gui` 闭包内可直接引用)。

绑定搜索框输入:

```python
        entry.bind("<KeyRelease>", lambda e: _apply_filter(search_var.get()))
```

- [ ] **Step 6: 手动验证**

重启灯窗口 → 右键「快捷记录」→ `☰` → 在搜索框输入一个已知记录里的词:
- 不匹配的条目应实时消失,匹配的留下;标题显示「匹配 X / 共 Y 条」。
- 清空搜索框 → 全部恢复,标题回到「共 Y 条」。
- 滚动条仍按需。

(本任务还无高亮、预览还是前 14 字 —— Task 3 补。)

- [ ] **Step 7: 提交**

```bash
git add cc_light.py
git commit -m "feat: 历史窗口搜索框 + 实时筛选 + 标题计数" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: 预览换 `Text` + 动态片段 + 命中高亮

**Goal:** 预览从 `Label` 换只读 `Text`,用 `preview_snippet` 动态显示命中片段,命中词黄底高亮。

**Files:**
- Modify: `cc_light.py` 的 `show_history`(row 创建段 + `_apply_filter`)

**Interfaces:**
- Consumes: `preview_snippet`(Task 1)。
- Produces: row 元组第三项为 `Text` 控件 `ptxt`(含 `hl` tag)。

- [ ] **Step 1: row 的预览 `Label` → 只读 `Text`**

定位原来创建 `t_lbl`(预览 `Label`)的段:

```python
                t_lbl = tk.Label(row, text=preview, fg="#dddddd", bg=BG,
                                 font=("Microsoft YaHei", 9), wraplength=300, justify="left")
                t_lbl.pack(side="left", fill="x", expand=True)
                t_labels.append(t_lbl)
```

替换为:

```python
                ptxt = tk.Text(row, bg=BG, fg="#dddddd", bd=0, highlightthickness=0,
                               wrap="word", font=("Microsoft YaHei", 9), height=1,
                               padx=0, pady=0, cursor="hand2")
                ptxt.pack(side="left", fill="x", expand=True)
                ptxt.tag_configure("hl", background="#6a5a1a", foreground="#ffffff")
                snippet0, _ = preview_snippet(text, "")
                ptxt.insert("1.0", snippet0)
                ptxt.config(state="disabled")
```

同时:
- 删掉原来的 `preview = flat[:14] + ...` 两行(片段改由 `preview_snippet` 生成);`flat = " ".join(text.split())` 若仅用于旧 preview 可一并删。
- 把后面 `_click`/`_right`/`row.bind`/`t_lbl.bind` 里的 `t_lbl` 全部替换为 `ptxt`。
- `rows.append((it, row, ptxt))`(Task 2 Step 2 写的是 `t_lbl`,这里统一成 `ptxt`)。
- 原 `t_labels` 列表及 `_on_cv_cfg` 里对它的 `wraplength` 设置删除(Text 用 `wrap="word"` 自适应,不需要 wraplength)。

- [ ] **Step 2: Text 高度自适应辅助函数**

在 `_sync_sb` 附近插入:

```python
        def _resize_text(t):
            try:
                t.update_idletasks()
                r = t.count("1.0", "end-1c", "displaylines")
                t.config(height=max(1, r[0] if r else 1))
            except Exception:
                pass
```

- [ ] **Step 3: `_apply_filter` 升级为高亮版**

把 Task 2 的 `_apply_filter` 替换为:

```python
        def _apply_filter(query):
            q = (query or "").strip()
            matched = 0
            for it, row, ptxt in rows:
                text = it.get("text", "")
                if match_record(text, q):
                    if not row.winfo_ismapped():
                        row.pack(side="top", fill="x", padx=2, pady=2)
                    matched += 1
                    snippet, spans = preview_snippet(text, q)
                    ptxt.config(state="normal")
                    ptxt.delete("1.0", "end")
                    ptxt.insert("1.0", snippet or "")
                    ptxt.tag_remove("hl", "1.0", "end")
                    for a, b in spans:
                        ptxt.tag_add("hl", "1.0+%dc" % a, "1.0+%dc" % b)
                    ptxt.config(state="disabled")
                    _resize_text(ptxt)
                else:
                    if row.winfo_ismapped():
                        row.pack_forget()
            if q:
                title_lbl.config(text=" 快捷记录历史 · 匹配 %d / 共 %d 条" % (matched, len(rows)))
            else:
                title_lbl.config(text=" 快捷记录历史(共 %d 条)" % len(rows))
            _sync_sb(cv.winfo_height())
```

- [ ] **Step 4: 窗口缩放时重算可见 Text 高度**

在 `_on_cv_cfg` 末尾(`_sync_sb(e.height)` 之前)加:

```python
            for it, row, ptxt in rows:
                if row.winfo_ismapped():
                    _resize_text(ptxt)
```

- [ ] **Step 5: 手动验证**

重启 → ☰ → 搜一个出现在长文本中后段的词:
- 预览应显示命中片段(命中词前后各 ~8 字,头尾 `…`),命中词黄底高亮。
- 清空 → 预览回到前 14 字、无高亮。
- 缩放历史窗口宽度 → 多行预览高度自适应,不裁剪。

- [ ] **Step 6: 提交**

```bash
git add cc_light.py
git commit -m "feat: 历史预览换 Text + 命中片段动态高亮" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: placeholder 行为 + Esc + 边界(空历史 / 无匹配)+ 端到端验证

**Goal:** 搜索框灰字占位、首次聚焦清空;Esc 清空恢复;空历史不显示搜索框;无匹配显示提示;端到端走一遍。

**Files:**
- Modify: `cc_light.py` 的 `show_history`

- [ ] **Step 1: placeholder 状态 + 聚焦清空 + Esc**

把 Task 2 创建 `entry` 的段替换为(带 placeholder 状态机):

```python
        ph = {"on": True}
        search_var = tk.StringVar()
        entry = tk.Entry(win, textvariable=search_var, bg="#2a2a2a", fg="#e8e8e8",
                         insertbackground="#e8e8e8", relief="flat", bd=0,
                         font=("Microsoft YaHei", 9))
        entry.insert(0, "搜索…")
        entry.config(fg="#777777")
        entry.pack(side="top", fill="x", padx=6, pady=(0, 4))

        def _on_focusin(_e):
            if ph["on"]:
                entry.delete(0, "end")
                entry.config(fg="#e8e8e8")
                ph["on"] = False
        def _on_key(_e):
            ph["on"] = False
            _apply_filter(search_var.get())
        def _on_esc(_e):
            entry.delete(0, "end")
            ph["on"] = True
            entry.insert(0, "搜索…")
            entry.config(fg="#777777")
            _apply_filter("")
        entry.bind("<FocusIn>", _on_focusin)
        entry.bind("<KeyRelease>", _on_key)
        entry.bind("<Escape>", _on_esc)
```

(删掉 Task 2 里原来那条 `entry.bind("<KeyRelease>", ...)`,由这里的 `_on_key` 取代。)

- [ ] **Step 2: 空历史不显示搜索框**

在创建 `entry` 之前判断 `if not items:` 分支:空历史时**不创建** `top` 标题行/搜索框,直接显示原来的「(无记录)」Label。把 Task 2 的标题/搜索框创建包进 `if items:`:

```python
        if items:
            # ... 标题 top + title_lbl + entry + 绑定(Step 1 的全部内容)...
        # else: items 为空,原「(无记录)」分支保持
```

(`rows` 为空时 `_apply_filter` 不会被调用,因为 entry 不存在;安全。)

- [ ] **Step 3: 无匹配提示**

在 `inner` 里加一个隐藏的提示 Label,`_apply_filter` 里按 matched 控制:

```python
        nomatch = tk.Label(inner, text="(无匹配记录)", fg="#888888", bg=BG,
                           font=("Microsoft YaHei", 9))
```

在 `_apply_filter` 末尾(`_sync_sb` 之前)加:

```python
            if q and matched == 0:
                nomatch.pack(side="top", anchor="w", padx=4, pady=8)
            else:
                nomatch.pack_forget()
```

- [ ] **Step 4: 端到端手动验证**

重启灯窗口 → 右键「快捷记录」→ `☰`:
1. 搜索框显示灰字「搜索…」;点进去自动清空变黑字光标。
2. 输入词:实时筛选、命中片段高亮、标题「匹配 X / 共 Y 条」。
3. 输入不存在的词:列表「(无匹配记录)」。
4. 按 `Esc`:清空、恢复占位「搜索…」、列表恢复全部。
5. 点某条:回填便签(便签里出现该文本)。
6. 右键某条 → 删除:该条消失、列表刷新。
7. 清空所有记录后重开历史:无搜索框,只显示「(无记录)」。

- [ ] **Step 5: 提交**

```bash
git add cc_light.py
git commit -m "feat: 历史搜索 placeholder/Esc + 空历史/无匹配边界" -m "Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review(写完后已自查)

- **Spec 覆盖**:UI/交互(Task 2/3/4)、匹配规则与动态片段(Task 1 纯函数 + Task 3 应用)、显隐不重建(Task 2 `_apply_filter` pack/pack_forget)、标题计数(Task 2)、预览 Text 高亮(Task 3)、placeholder/Esc/空历史/无匹配(Task 4)、纯函数单测 + 端到端手动(Task 1/Task 4)—— spec 各节均有任务对应。
- **Placeholder**:无 TBD/TODO;每个代码步骤都给了完整可粘贴代码。
- **类型/命名一致**:`match_record` / `preview_snippet` 全程一致;row 元组 `(it, row, ptxt)` 在 Task 2→3 统一为 `ptxt`(`t_lbl` 仅 Task 2 中间态,Task 3 Step 1 明确改名)。
