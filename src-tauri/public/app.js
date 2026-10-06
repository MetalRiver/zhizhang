/* Usage Ledger desktop boot page — R10-D。
   只做三件事：监听壳事件更新状态 / 失败时展示最小错误卡 / 三个受限动作。 */
(function () {
  'use strict';
  var statusEl = document.getElementById('status');
  var statusText = document.getElementById('statusText');
  var bar = document.getElementById('bar');
  var card = document.getElementById('errcard');
  var sig = document.getElementById('sig');

  function setStatus(text, isError) {
    statusText.textContent = text;
    statusEl.classList.toggle('err', !!isError);
  }

  function showError(detail) {
    sig.classList.remove('spin');
    bar.classList.add('done');
    card.classList.add('show');
    if (detail) {
      document.getElementById('errDetail').innerHTML = detail;
    }
  }

  var tauri = window.__TAURI__;
  if (!tauri || !tauri.event) {
    // 理论上不会发生（withGlobalTauri 已开启）；兜底提示而非白屏。
    setStatus('桌面壳接口初始化失败，请重启应用。', true);
    showError();
    return;
  }

  tauri.event.listen('backend-status', function (e) {
    setStatus(String(e.payload || ''), false);
  });

  tauri.event.listen('backend-failed', function (e) {
    var detail = String((e && e.payload) || '');
    setStatus('启动失败', true);
    showError(detail);
  });

  document.getElementById('btnRetry').addEventListener('click', function () {
    card.classList.remove('show');
    sig.classList.add('spin');
    bar.classList.remove('done');
    setStatus('正在重试启动本地服务…', false);
    tauri.event.emit('backend-retry');
  });

  document.getElementById('btnLogs').addEventListener('click', function () {
    tauri.core.invoke('open_data_root');
  });

  document.getElementById('btnExit').addEventListener('click', function () {
    tauri.event.emit('backend-exit');
  });
})();
