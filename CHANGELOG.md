# Changelog

All notable changes to **n.e.k.o_plugin_rvc_singer** (A-side plugin) are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/).

---

## [0.0.3] - 2026-07-29

### Added
- Initial marketplace release of the A-side NEKO plugin
- 联网搜索下载音频（网易云 / YouTube / Bilibili）
- 人声分离 + 翻唱 + 混音推送（依赖 B 端 RVC Studio）
- 悬浮歌词窗口（PySide6 磨砂背板 + 卡拉OK 逐字高亮）
- 悬浮队列窗口（实时进度 + 单独取消 + 历史记录）
- 健康检查 + 自动重连（30 秒心跳 + 10 秒离线加速轮询）
- HTTP 连接池复用 / 鉴权头注入 / SSL 自签证书支持
- 参数校验 + 类型检查
- 本地缓存机制（songs / models 列表，5 分钟 TTL）
- 并发控制 + 任务去重（队列引擎）
- 错误分类 + 自适应恢复（指数退避重试）
- 关键词触发：唱首歌 / 唱一首 / 翻唱 / 对比 / 试听 / search song …
- i18n：`zh-CN`（默认）/ `en`

### Dependencies
- Python ≥ 3.10
- `aiohttp` ≥ 3.9
- `PySide6` ≥ 6.6 (optional — 缺失时歌词/队列窗口降级为 no-op)
- 配套 B 端：**RVC Studio 0.1.0+** (UI 0.2.0+)
- 底层 RVC 引擎：**RVC1006Nvidia**（花儿不哭大佬的整合包）

### Known Limitations
- 不支持 50 系 N 卡（CUDA 12 + sm_120 兼容性问题）
- B 端 RVC Studio 必须独立运行（端口 19877）
- 联网搜索需在 B 端配置 yt-dlp 凭据（YouTube 部分视频需登录）
