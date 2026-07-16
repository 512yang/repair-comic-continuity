# 漫画审校工作台

这是 `repair-comic-continuity` 的 Codex 插件版本。它把现有 Skill、漫画圈画批注界面和本地 MCP 桥接工具放在一个可安装包中。

第一版支持两种运行入口：

- `human_visual_auto_text`：人工逐页判断画面问题，内嵌文字引擎仍检查全部页面；
- `automatic`：Skill 自动检查画面，处理后仍要求人工逐页复核全部输出。

工作台只负责交互和安全持久化。小说对齐、人物与场景连续性、无字整页重绘、文字修复、经验学习和最终发布门禁仍由内嵌 Skill 负责。

## 开发验证

```powershell
npm run typecheck
npm test
python -m unittest discover -s '.\skills\repair-comic-continuity\tests' -p 'test_*.py'
npm run validate:plugin
```

设计与实施计划位于 `docs/superpowers/`。
