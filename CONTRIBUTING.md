# 参与贡献

感谢你愿意改进 PDF Size Reducer。

## 提交问题

请说明：

- Windows 版本与程序版本；
- 原 PDF 的大致页数、文件大小和图形类型；
- 目标大小、实际结果和完整错误信息；
- 是否可以提供最小化、已脱敏的复现 PDF。

论文可能包含未公开内容。请勿在公开 Issue 中上传敏感或未授权的 PDF。

## 本地开发

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe qt_app.py
```

## Pull Request

1. 从 `main` 创建小而聚焦的分支；
2. 为压缩行为变化添加回归测试；
3. 不要降低 180 DPI 的可读性底线或破坏原生文字层；
4. 更新 `CHANGELOG.md` 中的对应说明；
5. 确保全部测试通过后再提交 Pull Request。
