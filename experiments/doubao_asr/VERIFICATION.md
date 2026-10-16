# 验证记录

日期：2026-10-16（Asia/Shanghai）。只使用专门合成的测试音频。

## 迁移后的代码

在独立 worktree 的 Python 3.11 conda 环境中安装 `websocket-client==1.8.0` 后运行：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q app_paths.py backend.py server.py desktop.py desktop_service.py doctor.py example_openai.py packaging tests experiments
python -m experiments.doubao_asr .\tests\fixtures\test.wav --json --verbose
git diff --check
```

- 现有测试与新增测试合计 **53 项通过**。新增的 7 项离线测试覆盖固定 protobuf 字节、未知字段、截断/溢出、首页解析变化、无效音频、最终结果、拒绝/超时/缺失最终结果和连接清理。
- 编译检查通过。
- 迁移后真实调用成功：程序通过空 CookieJar 的正常首页请求取得新游客标识，没有读取浏览器资料或提供登录凭据。
- 测试 WAV 时长 **6.28 秒**，最终文字为“你好，这是微信语音接口测试。今天阳光很好。”。该内容是测试录音中的语句；实际识别服务为豆包。
- 本次 CLI JSON 记录的总耗时 **7.75 秒**，包含首页初始化、实时速率发送音频及结束会话。这个单次值不是延迟保证。
- 收到了 `TaskStarted`、`SessionStarted`、最终 `ASRResponse`、`ASREnded` 和 `SessionFinished`，业务状态码为 `20000000`。
- `git diff --check` 通过。原始运行日志保留在被忽略的 `.runtime/`，没有把设备标识、Cookie、抓取页面或个人浏览器参数加入源码。

未安装可选 `websocket-client` 时，根目录测试仍可运行协议和首页解析测试，会跳过依赖该组件的会话模拟测试。运行上面的额外依赖安装后，新增测试全部执行。

## 此前原型验证

迁移前已用 12.62 秒的合成语音完成独立转写，并比较过旧浏览器标识、退出登录后的旧标识，以及普通首页请求正常初始化的全新游客标识。

这些是历史原型结果；上面的 6.28 秒验证才直接对应此目录内整理后的实现。没有把网页截图、账号关联设备标识或下载的厂商 JavaScript 文件作为项目依赖。

## 未覆盖的范围

未做微信/微信输入法原生运行复验；本变更没有修改它们的后端。现有相关测试是离线/模拟检查，不能当作客户端运行证明。

尚未验证豆包的长录音、并发、断网恢复、游客标识有效期、跨网络表现、长期运行或服务端限额。服务端与网页结构可能更新，成功记录只说明测试当时的可用性。
