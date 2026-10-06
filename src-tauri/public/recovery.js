// recovery.js — 重新连接账本（RC.3 §38/§39）
// kind: missing_root（bootstrap 指向的位置失联）| corrupt_bootstrap（引导文件损坏）
(function () {
  var info = { kind: 'missing_root', data_root: '', message: '' };
  function invoke(cmd, args) {
    var t = window.__TAURI__;
    var f = t && (t.core && t.core.invoke || t.invoke);
    if (!f) return Promise.reject(new Error('tauri ipc unavailable'));
    return f(cmd, args);
  }
  function status(text) {
    document.getElementById('status').textContent = text || '';
  }
  function render() {
    document.getElementById('last-root').textContent =
      info.data_root || '（未知）';
    if (info.kind === 'corrupt_bootstrap') {
      document.getElementById('title').textContent = '账本引导信息需要修复';
      document.getElementById('reason').textContent =
        '引导文件损坏（' + (info.message || '未知原因') + '）。' +
        '如果你只有一个账本位置，选择它即可继续；无法确定时请选择账本位置。';
    } else {
      document.getElementById('reason').textContent =
        '上次使用：' + (info.data_root || '未知位置');
    }
  }
  function choose(btn) {
    btn.disabled = true;
    invoke('pick_folder').then(function (picked) {
      btn.disabled = false;
      if (!picked) return;
      status('正在连接所选位置…');
      invoke('set_data_root', { path: picked })
        .then(function (r) {
          status(r && r.adopted
            ? '已连接到该位置的现有账本，正在打开…'
            : '已设定账本位置，正在打开…');
          // Rust 侧已切换并重启 backend；页面将被导航到账本首页
        })
        .catch(function (e) {
          status('');
          alert('无法使用该位置：' + e);
        });
    }).catch(function () { btn.disabled = false; });
  }
  document.getElementById('btn-retry').addEventListener('click', function () {
    var btn = this;
    btn.disabled = true;
    status('正在重新尝试…');
    invoke('retry_recovery').then(function (r) {
      btn.disabled = false;
      if (r && r.ok) { status('已找到账本，正在打开…'); }
      else { status('仍不可用。请确认磁盘已接通，或选择账本位置。'); }
    }).catch(function () { btn.disabled = false; status('重试失败，请稍后再试'); });
  });
  document.getElementById('btn-choose').addEventListener('click', function () {
    choose(this);
  });
  document.getElementById('btn-fresh').addEventListener('click', function () {
    choose(this);
  });
  document.getElementById('btn-open').addEventListener('click', function () {
    invoke('open_data_root').catch(function () {});
  });
  invoke('get_boot_info').then(function (r) {
    if (r && r.kind && r.kind !== 'normal') info = r;
    render();
  }).catch(render);
})();
