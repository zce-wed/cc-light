# cc-light hook 元数据定位:输出一行 "hwnd<TAB>termpid"。环境变量 PREV_HWND/PREV_TPID/NAME。
# 定位规则(按优先级):
#  1. 旧 hwnd 存活 且 其所属进程仍在当前进程父链上(归属校验) → 保留。
#     归属校验治「错值不自愈」:旧 hwnd 指向的窗口(如 Trae)虽活着,但不在本会话进程树
#     (claude→powershell→WindowsTerminal)上 → 判失效强制重取,resume/迁移场景的污染自愈。
#  2. GetForegroundWindow 快速路径:hook 触发瞬间前台是终端类进程 → 记之(用户正盯着终端)。
#  3. 父链遍历(termpid 是 powershell 无窗口,窗口在祖父 WT/Trae 主进程):
#     - 命中 WindowsTerminal:单进程多窗口,MainWindowHandle 只是其中一个(通常是最早开的)
#       → EnumWindows 枚举该 pid 全部可见顶层窗口,标题含项目名(NAME)者优先
#         (TAPD-exec <项目名> 命中),无匹配再退 MainWindowHandle;
#     - 其余终端类(Trae/VSCode 等):直接 MainWindowHandle。
# termpid:旧进程仍在父链上 → 保留;否则父链找 name 含 claude 的进程取其父。
$ErrorActionPreference = "SilentlyContinue"
Add-Type -MemberDefinition '
[DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lp);
public delegate bool EnumWindowsProc(IntPtr h, IntPtr lp);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder sb, int n);
[DllImport("user32.dll")] public static extern int GetWindowTextLengthW(IntPtr h);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
' -Name U2 -Namespace CCL -PassThru | Out-Null

# 当前进程父链(有序,近→远,去重防循环)。统一 [int] 入链:CIM 的 ParentProcessId 是 UInt32,
# ArrayList.Contains 严格类型比较 Int32≠UInt32 恒 false(曾致归属校验形同虚设)。
# 系统进程即停:进程退出后父 pid 被复用,链穿过 svchost/system 后会"接"到无关进程
# (如 Trae)的 pid 上,把错窗口骗过归属校验——终端链全在用户态层级,到 svchost 必是假链。
$chain = New-Object System.Collections.ArrayList
$p = $PID
while ($p -and -not ($chain -contains [int]$p) -and $chain.Count -lt 12) {
    $pr = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $p)
    if (-not $pr) { break }
    if ($pr.Name -match '^(svchost|services|wininit|csrss|smss|lsass|dwm|fontdrvhost)\.exe$') { break }
    [void]$chain.Add([int]$p)
    $p = $pr.ParentProcessId
}

$term = "WindowsTerminal|conhost|cmd|Code|Trae|Cursor|Windsurf|wezterm|alacritty|Hyper|conemu64|HBuilderX"

# ---- hwnd ----
$hwnd = [int64]0
$prev = [int64]$env:PREV_HWND
if ($prev -and [CCL.U2]::IsWindow([IntPtr]$prev)) {
    $wp = [uint32]0
    [void][CCL.U2]::GetWindowThreadProcessId([IntPtr]$prev, [ref]$wp)
    if ($wp -and ($chain -contains [int]$wp)) { $hwnd = $prev }
}
if (-not $hwnd) {
    # 前台法也做链校验:前台窗口属于本会话进程树(用户正盯着自己的终端)才可信;
    # 否则前台 Trae/浏览器时会把无关窗口错记成本会话窗口。
    $fg = [CCL.U2]::GetForegroundWindow()
    if ($fg -and $fg -ne [IntPtr]::Zero) {
        $fpv = [uint32]0
        [void][CCL.U2]::GetWindowThreadProcessId($fg, [ref]$fpv)
        if ($fpv -and ($chain -contains [int]$fpv)) {
            $fp = Get-Process -Id $fpv
            if ($fp -and $fp.Name -match $term) { $hwnd = [int64]$fg }
        }
    }
}
if (-not $hwnd) {
    foreach ($cp in $chain) {
        $proc = Get-Process -Id $cp
        if (-not $proc -or -not $proc.MainWindowHandle -or $proc.MainWindowHandle -eq 0) { continue }
        if ($proc.Name -match "WindowsTerminal") {
            $script:wtPid = $cp
            $script:cands = New-Object System.Collections.ArrayList
            $cb = [CCL.U2+EnumWindowsProc]{
                param($h, $l)
                if ([CCL.U2]::IsWindowVisible($h) -and [CCL.U2]::GetParent($h) -eq [IntPtr]::Zero) {
                    $tp = [uint32]0
                    [void][CCL.U2]::GetWindowThreadProcessId($h, [ref]$tp)
                    if ($tp -eq $script:wtPid) { [void]$script:cands.Add($h) }
                }
                return $true
            }
            [void][CCL.U2]::EnumWindows($cb, [IntPtr]::Zero)
            $nm = [string]$env:NAME
            $pick = [IntPtr]::Zero
            if ($nm.Length -ge 2) {
                foreach ($wh in $script:cands) {
                    $len = [CCL.U2]::GetWindowTextLengthW($wh)
                    if ($len -le 0) { continue }
                    $sb = New-Object System.Text.StringBuilder ($len + 1)
                    [void][CCL.U2]::GetWindowTextW($wh, $sb, $sb.Capacity)
                    if ($sb.ToString().ToLower().Contains($nm.ToLower())) { $pick = $wh; break }
                }
            }
            if ($pick -eq [IntPtr]::Zero -and $script:cands.Count -gt 0) { $pick = $script:cands[0] }
            if ($pick -ne [IntPtr]::Zero) { $hwnd = [int64]$pick }
            else { $hwnd = [int64]$proc.MainWindowHandle }
            break
        }
        if ($proc.Name -match $term) { $hwnd = [int64]$proc.MainWindowHandle; break }
    }
}

# ---- termpid ----
$tpid = [int64]0
$prevT = [int64]$env:PREV_TPID
if ($prevT -and ($chain -contains [int]$prevT)) { $tpid = $prevT }
if (-not $tpid) {
    foreach ($cp in $chain) {
        $proc2 = Get-Process -Id $cp
        if ($proc2 -and $proc2.Name -match "claude") {
            $pr2 = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $cp)
            if ($pr2) { $tpid = [int64]$pr2.ParentProcessId }
            break
        }
    }
}
[Console]::Write($hwnd.ToString() + "`t" + $tpid.ToString())
