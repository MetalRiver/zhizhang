// close-ask.js — 关闭行为询问对话框（RC.3 §9）
// 按钮把选择交回 Rust 壳（resolve_close_choice）；取消（×）= 什么都不发生。
(function () {
  function invoke(cmd, args) {
    var t = window.__TAURI__;
    if (t && t.core && t.core.invoke) return t.core.invoke(cmd, args);
    if (t && t.invoke) return t.invoke(cmd, args);
    return Promise.reject(new Error('tauri ipc unavailable'));
  }
  function choose(choice) {
    var remember = document.getElementById('remember').checked;
    invoke('resolve_close_choice', { choice: choice, remember: remember })
      .catch(function () { /* IPC 失败：保持现状，不误退 */ });
  }
  document.getElementById('btn-background').addEventListener('click', function () {
    choose('background');
  });
  document.getElementById('btn-exit').addEventListener('click', function () {
    choose('exit');
  });
})();
