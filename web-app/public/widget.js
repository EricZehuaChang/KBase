/**
 * KBase 网站嵌入 widget（对标 AnythingLLM embed / MaxKB 零代码嵌入）。
 * 用法（任意网站 HTML 里一行）：
 *   <script src="https://<kbase-host>/widget.js" data-kbase-share="<token>" defer></script>
 * 效果：右下角浮动问答球，点开是 iframe 加载的免登录分享页（/share/<token>?embed=1）。
 * 零依赖纯原生 JS；样式全部内联，不污染宿主页面（仅两个固定定位元素）。
 * data-kbase-host 可选：iframe 指向的 KBase 地址，缺省取本脚本的来源站点。
 */
(function () {
  var script = document.currentScript;
  if (!script) return;
  var token = script.getAttribute("data-kbase-share");
  if (!token) { console.warn("[kbase-widget] 缺少 data-kbase-share"); return; }
  var host = script.getAttribute("data-kbase-host")
    || new URL(script.src).origin;

  // 浮动球
  var btn = document.createElement("button");
  btn.setAttribute("aria-label", "打开智能问答");
  btn.innerHTML = "&#128172;"; // 💬
  btn.style.cssText = [
    "position:fixed", "right:24px", "bottom:24px", "z-index:2147483000",
    "width:56px", "height:56px", "border-radius:50%", "border:none",
    "background:#534AB7", "color:#fff", "font-size:24px", "cursor:pointer",
    "box-shadow:0 4px 16px rgba(0,0,0,.25)", "transition:transform .15s",
  ].join(";");
  btn.onmouseenter = function () { btn.style.transform = "scale(1.06)"; };
  btn.onmouseleave = function () { btn.style.transform = "scale(1)"; };

  // 问答面板（iframe 懒创建：不点不加载，零性能税）
  var panel = null;
  function togglePanel() {
    if (panel) {
      var show = panel.style.display === "none";
      panel.style.display = show ? "block" : "none";
      btn.innerHTML = show ? "&#10005;" : "&#128172;"; // ✕ / 💬
      return;
    }
    panel = document.createElement("div");
    panel.style.cssText = [
      "position:fixed", "right:24px", "bottom:92px", "z-index:2147483000",
      "width:400px", "height:600px", "max-width:calc(100vw - 32px)",
      "max-height:calc(100vh - 120px)", "border-radius:14px",
      "overflow:hidden", "box-shadow:0 12px 40px rgba(0,0,0,.3)",
      "background:#fff",
    ].join(";");
    var frame = document.createElement("iframe");
    frame.src = host + "/share/" + token + "?embed=1";
    frame.style.cssText = "width:100%;height:100%;border:none";
    frame.setAttribute("title", "KBase 智能问答");
    panel.appendChild(frame);
    document.body.appendChild(panel);
    btn.innerHTML = "&#10005;";
  }
  btn.onclick = togglePanel;
  document.body.appendChild(btn);
})();
