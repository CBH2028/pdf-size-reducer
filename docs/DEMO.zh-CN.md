<div align="right">

[English](DEMO.md) | **简体中文**

</div>

# 操作演示与大型 PDF 压力测试

[返回中文项目主页](../README.zh-CN.md) · [下载 Windows 版](https://github.com/CBH2028/pdf-size-reducer/releases/latest)

## 完整操作视频

[![PDF Size Reducer 操作演示](media/operation-demo.gif)](https://github.com/CBH2028/pdf-size-reducer/releases/download/v3.3.0/PDF_Size_Reducer_Operation_Demo.mp4)

视频使用 v3.3.0 的真实商业化程序界面录制，完整展示以下过程：

1. 后台读取 97.92 MiB、48 页的合成科研 PDF；
2. 在主界面浏览 48 个完整 Figure 的全景缩略图与估算占用；
3. 打开约 240 DPI 高清预览并放大检查细线；
4. 取消不需要处理的 Figure，输入精确目标大小；
5. 执行任务并确认输出文件生成。

[下载高清 MP4（1440 × 900，19 秒）](https://github.com/CBH2028/pdf-size-reducer/releases/download/v3.3.0/PDF_Size_Reducer_Operation_Demo.mp4)

## 演示 PDF

[下载 `PDF_Size_Reducer_Stress_Demo_97.92MB.pdf`](https://github.com/CBH2028/pdf-size-reducer/releases/download/v3.2.0/PDF_Size_Reducer_Stress_Demo_97.92MB.pdf)

该文件由仓库中的压力测试生成器创建，不包含真实论文或个人信息：

| 指标 | 数值 |
| --- | ---: |
| 文件大小 | 102,676,547 bytes（97.92 MiB） |
| 页数 | 48 |
| 完整 Figure | 48 |
| 每页高熵位图 | 1600 × 1000 |
| 每页矢量路径 | 90 |
| SHA-256 | `C0481F39FE607C42E66E30E36266FE8440CBBE518FCE38A48670CE9B1E851214` |

## 已测性能

在 v3.3.0 测试环境中，扫描用时约 6.62 秒，扫描加全部缩略图约 8.86 秒；Qt 事件循环最大停顿约 41.16 ms，主进程峰值内存约 113.11 MiB。界面在处理期间保持响应，并可使用“停止读取”安全取消。

机器配置、磁盘速度和 PDF 结构会影响实际结果。这些数据用于回归比较，不代表所有设备的固定耗时。

## 复现

运行压力测试：

```powershell
.\.venv\Scripts\python.exe .\tools\stress_test.py `
    --pages 48 --image-width 1600 --image-height 1000 `
    --vector-paths 90 --timeout 300
```

对已有 PDF 执行相同的界面响应测试：

```powershell
.\.venv\Scripts\python.exe .\tools\stress_test.py `
    --pdf "D:\path\large.pdf" --timeout 300
```

操作视频由 `tools/record_demo.py` 从 Qt 控件内部抓帧，不录制桌面，因此不会把通知、其他窗口或个人桌面内容带入成片。
