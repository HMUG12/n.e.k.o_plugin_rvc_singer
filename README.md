# n.e.k.o_plugin_rvc_singer

> 让 N.E.K.O 用训练好的 RVC 模型唱歌 | Let N.E.K.O sing with trained RVC voice models

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.0.3-blue.svg)](CHANGELOG.md)
[![NEKO SDK](https://img.shields.io/badge/NEKO_SDK-0.1.0+-orange.svg)](plugin.toml)

N.E.K.O 桌面端的 RVC 歌声合成插件。配合独立运行的 RVC Studio（B 端），实现从「说一句话」到「完整演唱」的端到端工作流：联网搜歌 → 下载音频 → 人声分离 → 音色转换 → 推送到 NEKO 对话并驱动口型同步。

---

## 简介

本插件采用 **A + B 端分离架构**：

| 端 | 项目 | 职责 |
|----|------|------|
| **A 端**（本仓库） | `n.e.k.o_plugin_rvc_singer` | NEKO 桌面端插件，负责上下文、对话、UI 浮窗 |
| **B 端** | [NEKO_rvcsinger-B](https://github.com/HMUG12/NEKO_rvcsinger-B) | RVC Studio 桌面程序，负责实际音频处理 |

> **A 端不直接处理音频**，所有 RVC / UVR5 推理都委托给 B 端；A 端只负责调度、UI、数据推送、健康检查、缓存与错误恢复。

默认健康检查地址：`http://127.0.0.1:19877/api/health`

---

## Introduction (English)

**n.e.k.o_plugin_rvc_singer** is the NEKO desktop plugin for AI singing voice conversion. It pairs with **RVC Studio** (a separate desktop application) to deliver a complete pipeline:

1. Parse user intent ("sing a song" / "cover X" / "compare A vs B")
2. Search & download the source audio (NetEase Cloud Music / YouTube / Bilibili)
3. Forward to RVC Studio for vocal separation, pitch shifting, and voice conversion
4. Stream the processed audio + synchronized lyrics + viseme data back to NEKO
5. Drive lip-sync playback in the NEKO desktop UI

The two parts talk via a local HTTP API. The A-side (this plugin) handles scheduling, UI, health checks, and error recovery. The B-side (RVC Studio) handles the actual neural-network inference.

---

## Credits / 致谢

- **编辑 / Editor**: [@未知之致（Unfound Depth）](https://github.com/HMUG12)
- **RVC 整合包原大佬**: 花儿不哭
- **RVC-Neko 整合包**: @未知之致（Unfound Depth）
- **底层 RVC 引擎**: [RVC1006Nvidia](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)
- **UI / UX**: 0.05
- **RVC Studio (B 端)**: 0.1.0 / UI 0.2.0
- **预计占用空间**: 约 20 GB（含 RVC 模型 + 训练数据 + 缓存）

---

## License

本插件遵循 **MIT 开源协议**。详见 [LICENSE](LICENSE)。

```
MIT License — Copyright (c) 2026 未知之致
```

---

## 功能 / Features

### 1. 联网搜索下载音频

支持网易云音乐、YouTube、Bilibili 三大主流平台。用户说"下载首歌"或"找一首XX"即可自动完成搜索→试听→下载。

![联网搜索下载](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=NEKO%20desktop%20plugin%20search%20interface%20titled%20%E6%AD%8C%E6%9B%B2%E6%90%9C%E7%B4%A2%20with%20three%20tabs%20labeled%20%E7%BD%91%E6%98%93%E4%BA%91%20YouTube%20%E5%93%94%E5%93%A9%E5%93%94%E5%93%A9.%20Search%20bar%20at%20top.%20Below%20shows%206%20song%20result%20cards%20in%202x3%20grid%20with%20album%20art%20placeholder%2C%20song%20name%2C%20artist%2C%20download%20icon.%20Dark%20theme%20with%20cyan%20accents%2C%20glassmorphic%20cards%2C%20clean%20modern%20UI%20mockup%20style&image_size=landscape_16_9)

### 2. 人声分离 + 翻唱 + 混音

UVR5 模型（MDX-Net / Demucs / VR Arch）自动分离人声与伴奏；RVC 模型将人声转换为目标音色；最后自动混合伴奏 + 转换后的人声 + 混响效果，输出完整歌曲。

![人声分离与翻唱](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=NEKO%20plugin%20vocal%20separation%20processing%20interface%20titled%20%E4%BA%BA%E5%A3%B0%E5%88%86%E7%A6%BB%20UVR5.%20Center%20shows%20two%20large%20waveform%20panels%20side%20by%20side%20labeled%20%E5%8E%9F%E6%9B%B2%20and%20%E5%88%86%E7%A6%BB%E5%90%8E%20with%20detailed%20audio%20waveforms.%20Progress%20bar%20at%2073%25.%20Model%20selector%20showing%20MDX-Net.%20Dark%20theme%20with%20magenta%20and%20cyan%20accents%2C%20scientific%20audio%20processing%20UI&image_size=landscape_16_9)

### 3. 悬浮歌词窗口（卡拉OK 逐字高亮）

磨砂背板 + 自由拖拽 + 卡拉OK 式逐字高亮。歌词根据音频时间戳同步推进，每个字的发光效果实时跟随播放进度。

![悬浮歌词](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=Floating%20karaoke%20lyrics%20window%20overlaid%20on%20dimmed%20music%20album%20background.%20Window%20has%20frosted%20glass%20glassmorphism%20effect%2C%20rounded%20corners.%20Shows%20%E2%9B%B5%20%E6%AD%A3%E5%9C%A8%E6%92%AD%E6%94%BE%20at%20top.%20Three%20lines%20of%20lyrics%20with%20middle%20line%20highlighted%20with%20bright%20cyan%20glow%20and%20character-by-character%20progress%20bar%20inside%20the%20text.%20Bottom%20shows%20progress%20bar%20at%2002%3A45%20%2F%2004%3A32.%20Dark%20theme%20with%20electric%20blue%20and%20pink%20accents%2C%20premium%20floating%20window%20design&image_size=landscape_16_9)

### 4. 悬浮队列窗口

实时显示正在处理 / 等待中 / 已完成 / 已失败的歌曲。正在处理的歌曲可一键取消；等待中的可单独移除；历史记录折叠在下方。

![悬浮队列](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=Floating%20song%20queue%20window%20on%20dark%20background%20titled%20%E6%AD%8C%E6%9B%B2%E9%98%9F%E5%88%97.%20Shows%204%20rows%3A%20row%201%20highlighted%20with%20progress%20bar%20at%2065%25%20and%20cancel%20button%2C%20rows%202-3%20in%20%E7%AD%89%E5%BE%85%E4%B8%AD%20state%2C%20row%204%20marked%20%E5%B7%B2%E5%AE%8C%E6%88%90.%20Glassmorphic%20frosted%20glass%20effect%2C%20rounded%20corners%2C%20current%20row%20glowing%20cyan.%20Clean%20modern%20dark%20UI%2C%20premium%20queue%20management%20interface&image_size=landscape_16_9)

### 5. 音色 A/B 对比

上传一段原声，A 端会调用 B 端用 N 个候选 RVC 模型各跑一遍，生成对比试听列表。直接在 NEKO 播放器面板里切 A/B 听效果，自动算音色相似度。

![音色对比](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=NEKO%20plugin%20A%20B%20model%20comparison%20interface%20titled%20%E9%9F%B3%E8%89%B2%E5%AF%B9%E6%AF%94%20with%20two%20audio%20waveform%20panels%20side-by-side%20labeled%20%E6%A8%A1%E5%9E%8B%20A%20and%20%E6%A8%A1%E5%9E%8B%20B.%20Below%20each%20waveform%20shows%20play%20button%2C%20model%20name%2C%20similarity%20percentage.%20Bottom%20shows%20verdict%20with%20green%20checkmark.%20Dark%20theme%20with%20purple-cyan%20gradient%2C%20scientific%20comparison%20UI%2C%20audio%20engineering%20aesthetic&image_size=landscape_16_9)

### 6. NEKO 集成面板

主面板显示 RVC Studio 连接状态、默认模型、当前队列、错误日志。用户可在此切换默认模型、修改连接地址、查看历史演唱记录。

![NEKO 集成面板](https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image?prompt=NEKO%20desktop%20plugin%20main%20panel%20UI%20titled%20RVC%20%E6%AD%8C%E5%A3%B0%E5%90%88%E6%88%90.%20Top%20section%20shows%20connection%20status%20with%20green%20dot%20labeled%20B%20%E7%AB%AF%E5%B7%B2%E8%BF%9E%E6%8E%A5%20and%20default%20model%20selector.%20Middle%20section%20shows%20song%20history%20with%20thumbnails.%20Right%20side%20shows%20action%20buttons%20for%20reconnect%20and%20config.%20Dark%20theme%20with%20cyan%20accents%2C%20modern%20dashboard%20layout%2C%20glassmorphic%20cards&image_size=landscape_16_9)

---

## 安装步骤 / Installation

### 前置要求 / Prerequisites

- **7-Zip 解压程序**（**必须**）— 用于解压 B 端安装包和模型包
- **Python ≥ 3.10**
- **NEKO 桌面端**（已装好）
- **B 端：RVC Studio 0.1.0+**（**必须**独立运行）
- **非 50 系 N 卡**（NVIDIA GeForce / Quadro / Tesla，CUDA 11.x–12.x）
- **预计硬盘占用**：20 GB（含 RVC 整合包 + 训练模型 + 缓存）

> ⚠️ **不兼容 NVIDIA 50 系显卡**（RTX 5090 / 5080 等），原因是 CUDA 12 + sm_120 与 PyTorch 2.x 的兼容性问题，参见 [issue #42](https://github.com/HMUG12/NEKO_rvcsinger-B/issues/42)。50 系用户请等待 PyTorch 官方支持。

### 安装 / Install

1. **安装 B 端 RVC Studio**

   从 [NEKO_rvcsinger-B Releases](https://github.com/HMUG12/NEKO_rvcsinger-B/releases) 下载最新版本，用 7-Zip 解压到任意目录（如 `E:\RVCStudio\`）。

2. **配置 B 端**

   ```bash
   # 首次启动：初始化 PyTorch + RVC 库（2-5 分钟）
   python rvc_studio_server.py
   ```

   看到 `RVC 引擎就绪` + `API 服务启动: http://127.0.0.1:19877` 即为成功。

3. **安装 A 端插件**

   在 NEKO 桌面端「我的插件」页面 → 「从本地安装」→ 选择本仓库 zip 包。

4. **验证连接**

   在 NEKO 对话里说「连接 RVC Studio」或「reconnect_studio」，A 端会自动 ping B 端 `/api/health`，看到 `studio_available: true` 即为成功。

5. **开始使用**

   直接在 NEKO 对话里说出下面的触发词即可。

---

## 与 B 端（RVC Studio）搭配使用

| A 端操作 | B 端响应 |
|----------|----------|
| `list_songs` | 返回本地歌曲库（SQLite） |
| `upload_song` | 接收 mp3/wav → 存到 `songs/` |
| `sing` | 选模型 → 调 RVC 推理 → 推送到 A 端 |
| `compare` | 用 N 个模型各跑一次 → 返回试听列表 |
| `train` | 启动 RVC 训练（GPU 重负载任务） |

**故障排查**：
- 「studio 未连接」→ 检查 B 端是否运行、防火墙是否放行 19877
- 「模型加载失败」→ 检查 `.pth` 文件是否在 `weights/`
- 「CUDA out of memory」→ 关闭 B 端其他 GPU 任务，或切换到 `is_half=False`

---

## 关键词触发示例 / Trigger Phrases

A 端通过关键词匹配识别用户意图。支持中、日、英、韩四语：

### 中文（zh-CN）

| 意图 | 触发词 |
|------|--------|
| 唱歌 | `唱首歌` / `唱一首` / `来一首` / `唱支歌` / `唱一下` |
| 翻唱 | `翻唱` / `cover` / `用XX的声音唱` |
| 找歌 | `找歌` / `搜歌` / `帮我找` / `找一首` |
| 下载 | `下载歌曲` / `下载音乐` / `下载这首` |
| 对比 | `对比` / `音色对比` / `A/B` / `AB对比` |
| 哪个模型 | `哪个模型` / `用什么唱` / `哪些模型` / `可用模型` |
| 试听 | `试听` / `听一下` |
| 切换模型 | `换模型` / `切换模型` / `用X模型唱` |

### 日本語（ja-JP）

| 意図 | トリガー |
|------|----------|
| 歌う | `歌って` / `歌って` / `歌って` |
| カバー | `カバー` / `cover` |

### English

| Intent | Trigger |
|--------|---------|
| Sing | `sing` / `sing a song` / `cover` |
| Search | `search song` / `find song` / `download` |
| Compare | `compare` / `A/B` / `which model` |
| Model | `model` / `voice` |

### 한국어（ko-KR）

| 의도 | 트리거 |
|------|--------|
| 노래 | `노래` / `노래 불러` / `커버` |

完整关键词列表见 [plugin.toml](plugin.toml) `keywords` 字段。

---

## 集成功能清单 / Integrated Features

- ✅ 联网搜索下载音频（网易云 / YouTube / Bilibili）
- ✅ 人声分离（UVR5：MDX-Net / Demucs / VR Arch）
- ✅ 翻唱（RVC 推理 + 音高偏移 + 索引检索）
- ✅ 自动混音（vocal_lead / inst_lead / live / general 四种预设）
- ✅ 悬浮歌词（卡拉OK 逐字高亮）
- ✅ 悬浮队列（实时进度 + 单独取消）
- ✅ 音色 A/B 对比
- ✅ 健康检查 + 自动重连
- ✅ 本地缓存（songs / models，5 分钟 TTL）
- ✅ i18n：简体中文 / English

---

## 开发 / Development

```bash
# 克隆
git clone https://github.com/HMUG12/n.e.k.o_plugin_rvc_singer
cd n.e.k.o_plugin_rvc_singer

# 安装依赖
pip install aiohttp pyside6

# 代码风格
ruff check .

# 运行测试
pytest tests/

# 提交前
neko-plugin check --release
```

---

## 路线图 / Roadmap

- [ ] 支持更多音源平台（QQ音乐 / 酷狗）
- [ ] 多语种歌词对齐（英语 / 日语 / 韩语卡拉OK）
- [ ] 实时变声器（直播场景）
- [ ] 模型市场（用户上传 / 分享训练好的 RVC 模型）
- [ ] Web UI 模式（脱离 NEKO 桌面端独立运行）

---

## 反馈 / Feedback

- 🐛 **Bug 报告**: [Issue Tracker](https://github.com/HMUG12/n.e.k.o_plugin_rvc_singer/issues/new?template=bug_report.md)
- 💡 **功能建议**: [Feature Request](https://github.com/HMUG12/n.e.k.o_plugin_rvc_singer/issues/new?template=feature_request.md)
- 💬 **讨论**: [GitHub Discussions](https://github.com/HMUG12/n.e.k.o_plugin_rvc_singer/discussions)

---

## 致谢 / Acknowledgments

- [花儿不哭](https://github.com/RVC-Project) — RVC 整合包原作者
- [RVC-Project](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — 底层 RVC 引擎
- [Project N.E.K.O](https://github.com/HMUG12) — 桌面端框架
- 所有 [贡献者](https://github.com/HMUG12/n.e.k.o_plugin_rvc_singer/graphs/contributors) ❤️
