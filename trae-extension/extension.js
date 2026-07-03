// cc-light-helper —— 配合 cc-light 悬浮灯,按终端 shell PID 精确切换终端。
// 触发 URI:  trae-cn://cc-light.cc-light-helper/focus?pid=<shell_pid>
// 扩展遍历所有终端,await term.processId,精确匹配的 show();匹配不到则聚焦终端面板。
const vscode = require('vscode');

async function eachTerm() {
    const out = [];
    for (const t of vscode.window.terminals) {
        let pid = undefined;
        try { pid = await t.processId; } catch (e) {}
        out.push({ term: t, pid: pid });
    }
    return out;
}

function activate(ctx) {
    // 诊断:列出所有终端的 processId + name(用于和 hook 记录的 termpid 对比)
    ctx.subscriptions.push(vscode.commands.registerCommand('cc-light-helper.diag', async () => {
        const items = await eachTerm();
        const msg = items.length
            ? items.map(it => `pid=${it.pid} name=${it.term.name}`).join(' | ')
            : '(无终端)';
        vscode.window.showInformationMessage(msg);
    }));

    ctx.subscriptions.push(vscode.window.registerUriHandler({
        async handleUri(uri) {
            const params = new URLSearchParams(uri.query || '');
            const want = parseInt(params.get('pid') || '0', 10);
            if (!want) {                                   // 没指定 pid:只聚焦终端面板
                vscode.commands.executeCommand('workbench.action.terminal.focus');
                return;
            }
            const items = await eachTerm();
            for (const it of items) {
                if (it.pid === want) {                     // 精确匹配 → 显示并聚焦该终端
                    it.term.show();
                    return;
                }
            }
            // 兜底:匹配不到(终端可能已关),聚焦终端面板
            vscode.commands.executeCommand('workbench.action.terminal.focus');
        }
    }));
}

function deactivate() {}

module.exports = { activate, deactivate };
