# 演示素材

此目录用于存放 README 顶部展示的截图与 GIF。

## 建议内容

1. **主界面截图**（`docs/screenshot-panel.png`）
   - 展示控制台整体布局：顶部账号下拉、导航标签、物资看板。

2. **流程演示 GIF**（`docs/demo.gif`）
   - 30 秒以内，建议录制：切换到另一个账号 → 打开排班策略 → 手动触发任务 → 模拟器启动 → 手机端收到微信战报。
   - 宽约 800px，文件控制在 5MB 以内（GitHub 对仓库内 GIF 有渲染限制）。

3. **投屏功能截图**（`docs/screenshot-cast.png`）
   - 展示实时触摸投屏界面。

## 引用方式

在 `README.md` 中使用：

```markdown
![主界面](docs/screenshot-panel.png)
![演示](docs/demo.gif)
```

## 录制建议

- 录屏工具：OBS、Peek（Linux）、或者直接用系统自带录屏。
- 转 GIF：`ffmpeg -i demo.mp4 -vf "fps=12,scale=800:-1" demo.gif`
- 注意录制前清空演示账号里的真实信息。