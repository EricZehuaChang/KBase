// 复制文本到剪贴板，跨"安全上下文"兼容。
//
// 为什么需要它：`navigator.clipboard` 仅在**安全上下文**（HTTPS 或
// localhost/127.0.0.1）下可用。局域网 http 或裸 IP 部署（如演示机
// http://1.95.86.187:5000）属于**非安全上下文**，`navigator.clipboard`
// 是 undefined——直接 `navigator.clipboard.writeText()` 会抛
// "Cannot read properties of undefined"，复制静默失败、按钮点了没反应
// （真机踩过：分享链接的"复制链接"按钮在 http 演示机上无效）。
//
// 回退：非安全上下文下用已废弃但兼容性仍覆盖该场景的 execCommand('copy')
// （临时不可见 textarea 选中后复制）。返回是否成功，由调用方决定提示文案。
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    // 非安全上下文回退
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
