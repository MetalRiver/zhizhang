# 贡献指南 · Contributing

感谢你对智账（PathOrbit AI Ledger）的关注！

## 开始之前

- 本项目许可证为 **GPL-3.0-only**（见 `LICENSE`）。提交贡献即表示你同意
  以相同许可授权你的贡献。
- 品牌名称与视觉资产**不随 GPL 授权**，请先阅读 `BRAND.md`。
  提交 UI 相关贡献时请勿引用品牌资产文件。

## 开发环境

```bash
# Python 后端（3.12）
python -m venv .venv
.venv\Scripts\pip install -r requirements-core.txt

# 前端 / Tauri CLI
npm ci

# 桌面壳测试（Rust）
cd src-tauri && cargo test

# 后端测试（仓库根目录）
.venv\Scripts\python -m unittest discover -s tests

# 桌面版构建（打包需要 PyInstaller：pip install -r requirements-build.txt）
npm run desktop:build
```

## 提交规范

- 一个提交做一件事；提交信息用一行说明变更意图。
- 不要提交：本机绝对路径、真实账本数据、截图、密钥、令牌
  （`.gitignore` 已覆盖常见项，仍请在提交前自查）。
- 不要在「关于」等处加入会与官方版本混淆的品牌元素（见 `BRAND.md`）。

## 报告问题

请附上：版本号、Windows 版本、复现步骤、预期/实际行为。
**不要**附上你的 usage.db、账本截图或任何真实使用数据。
