# RVC Singer 插件模块化重构规划

## 当前问题
- `__init__.py` 1971行，单文件承载：HTTP、缓存、任务、健康检查、状态推送、对比、UI、日志等全部职责
- 难以维护、测试、扩展

## 目标架构
```
rvc_singer/
├── __init__.py          # 仅 @plugin_entry 装饰入口 + 核心生命周期
├── http_client.py       # HTTP 请求封装（GET/POST/流式）
├── state.py             # 插件状态管理、健康检查、SDK error patch
├── tasks.py             # 任务提交、轮询、结果推送、进度追踪
├── compare.py           # A/B 对比逻辑
├── cache.py             # 缓存层（TTL、过期判断）
└── ui_panel.py          # @ui.context 数据提供、UI交互
```

## 模块职责划分

### 1. http_client.py
负责所有 HTTP 请求相关
- `AsyncHttpClient` 类：异步 GET/POST 封装
- 鉴权头注入（P2 api_key）
- SSL 证书验证控制
- 超时和重试逻辑
- User-Agent 设置

### 2. state.py
负责状态和健康管理
- `PluginState` 类：管理连接状态、错误统计、堆栈
- `HealthChecker` 类：定期检查 B 端可用性
- 状态转换逻辑（offline → connected → active）
- 最后错误记录、错误振荡防抖

### 3. cache.py
负责缓存层
- `CachedData` 类：TTL 过期检查
- `CacheLayer` 类：统一管理多种缓存（歌曲、模型、状态）
- Cache hit/miss 统计

### 4. tasks.py
负责任务管理和推送
- `TaskManager` 类：任务提交、去重、轮询
- `ProgressTracker` 类：进度推送、状态聚合
- 结果推送到 NEKO 对话
- 取消任务逻辑

### 5. compare.py
负责 A/B 对比
- `CompareManager` 类：对比任务管理
- 多模型并行处理
- 对比结果聚合

### 6. ui_panel.py
负责 UI 上下文数据
- `@ui.context` 提供器：返回歌曲列表、模型列表、任务进度
- UI 交互回调

### 7. __init__.py
最精简的入口
- `@neko_plugin` 装饰类
- `@lifecycle` 生命周期（startup/shutdown）
- `@plugin_entry` 主业务逻辑（调用其他模块）
- 模块依赖组装

## 重构优先级
1. 🟢 **立即做**：拆离 http_client.py（最独立）
2. 🟡 **次要**：拆离 state.py、cache.py（单一职责）
3. 🟠 **后续**：拆离 tasks.py、compare.py、ui_panel.py
