# 贡献

使用 Python 3.11 的 conda 环境，安装 `requirements.txt`。从 `developing` 分支开发并提交，通过 PR 合并到 `main`。

提交前运行：

```powershell
.\env\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\env\python.exe -m compileall -q backend.py server.py doctor.py example_openai.py tests
```

涉及原生调用、析构或对象布局时，必须在适用版本且已登录的真实微信中复验。只使用合成测试语音，记录命令、识别结果、会话清理和脚本卸载后微信是否正常。CI 不替代本机原生验证。

不要提交微信二进制、账号密钥、真实聊天录音、数据库、运行 PID、机器绝对路径或 `.runtime/` 内容。第三方组件不因本项目 MIT 许可而改变自己的许可证。
