# 贡献

使用 Python 3.11 的 conda 环境，安装 `requirements.txt`。从 `developing` 分支开发并提交，通过 PR 合并到 `main`。

提交前运行：

```powershell
.\env\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\env\python.exe -m compileall -q app_paths.py backend.py server.py desktop.py desktop_service.py doctor.py example_openai.py packaging tests
```

涉及原生调用、析构或对象布局时，必须在适用版本且已登录的真实微信中复验。只使用合成测试语音，记录命令、识别结果、会话清理和脚本卸载后微信是否正常。CI 不替代本机原生验证。

不要提交微信二进制、账号密钥、真实聊天录音、数据库、运行 PID、机器绝对路径或 `.runtime/` 内容。第三方组件不因本项目 MIT 许可而改变自己的许可证。

窗口版构建使用 `build.ps1`，构建依赖在 `requirements-build.txt`。PyInstaller 的输出位于 `build/` 和 `dist/`，均不进入 Git。打包后的原生调用可用 `tests/verify_portable.py --exe <完整 EXE 路径>` 验证；先正常停止其他连接微信的桥接服务，避免测试干扰。

窗口布局用 `packaging/render_preview.py --output <PNG 路径>` 直接渲染应用检查，支持默认/最小窗口尺寸及运行、停止、错误状态，无需桌面自动化。版本号集中在 `desktop_service.py`，发布前同步使用说明中的 ZIP 文件名。
