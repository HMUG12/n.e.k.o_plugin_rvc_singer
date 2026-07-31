# 🚀 RVC Singer 插件完整部署指南

**最后更新**: 2026-07-17  
**插件版本**: 2.0 Professional Edition  
**状态**: ✅ 生产就绪

---

## 📋 快速清单 (Quick Checklist)

### 部署前
- [ ] 已安装 NEKO 主程序 (v0.6+)
- [ ] Python 3.8+ 环境可用
- [ ] RVC Studio 独立程序已准备
- [ ] 网络连接正常

### 部署中
- [ ] 复制文件到 NEKO 插件目录
- [ ] 删除 `__pycache__/` 文件夹
- [ ] 验证文件结构完整
- [ ] 修改 plugin.toml 配置

### 部署后
- [ ] 启动 RVC Studio
- [ ] 重启 NEKO 主程序
- [ ] 打开插件管理面板验证
- [ ] 发送测试演唱请求

---

## 🔧 分步部署指南

### 第 1 步：准备环境

#### Windows

```powershell
# 打开 PowerShell (管理员)

# 检查 NEKO 安装目录
$nekoDir = "$env:LOCALAPPDATA\N.E.K.O"
if (Test-Path $nekoDir) {
    Write-Host "✓ NEKO 已安装: $nekoDir"
} else {
    Write-Host "✗ NEKO 未安装，请先安装"
    exit
}

# 检查插件目录
$pluginDir = "$nekoDir\plugins"
if (-not (Test-Path $pluginDir)) {
    New-Item -ItemType Directory -Path $pluginDir -Force | Out-Null
}
Write-Host "✓ 插件目录: $pluginDir"
```

#### Linux/macOS

```bash
# 检查 NEKO 安装
if [ ! -d "$HOME/.neko" ]; then
    echo "✗ NEKO 未安装"
    exit 1
fi

NEKO_DIR="$HOME/.neko"
PLUGIN_DIR="$NEKO_DIR/plugins"
mkdir -p "$PLUGIN_DIR"
echo "✓ 插件目录: $PLUGIN_DIR"
```

### 第 2 步：复制插件文件

#### Windows PowerShell

```powershell
# 源目录（本 rvc_singer 插件目录，即当前 README 所在的目录）
$sourceDir = "<RVC_PROJECT_DIR>\rvc_singer"

# 目标目录（NEKO 插件目录）
$targetDir = "$env:LOCALAPPDATA\N.E.K.O\plugins\rvc_singer"

# 复制文件（覆盖）
Write-Host "复制插件文件..."
Copy-Item "$sourceDir\*" $targetDir -Recurse -Force

Write-Host "✓ 文件已复制到: $targetDir"

# 验证关键文件
$requiredFiles = @("__init__.py", "plugin.toml", "ui\panel.tsx")
foreach ($file in $requiredFiles) {
    $path = Join-Path $targetDir $file
    if (Test-Path $path) {
        Write-Host "  ✓ $file"
    } else {
        Write-Host "  ✗ $file (缺失)"
    }
}
```

#### Linux/macOS Bash

```bash
SOURCE_DIR="$HOME/rvc-project/N.E.K.O-main/plugin/plugins/rvc_singer"
TARGET_DIR="$HOME/.neko/plugins/rvc_singer"

echo "复制插件文件..."
mkdir -p "$TARGET_DIR"
cp -r "$SOURCE_DIR/"* "$TARGET_DIR/"
echo "✓ 文件已复制"

# 验证
for file in __init__.py plugin.toml ui/panel.tsx; do
    if [ -f "$TARGET_DIR/$file" ]; then
        echo "  ✓ $file"
    else
        echo "  ✗ $file (缺失)"
    fi
done
```

### 第 3 步：清理缓存（重要！⚠️）

#### Windows PowerShell

```powershell
$targetDir = "$env:LOCALAPPDATA\N.E.K.O\plugins\rvc_singer"
$cacheDir = Join-Path $targetDir "__pycache__"

if (Test-Path $cacheDir) {
    Write-Host "删除旧缓存文件..."
    Remove-Item $cacheDir -Recurse -Force
    Write-Host "✓ 缓存已清理"
} else {
    Write-Host "✓ 无缓存文件"
}

# 也删除子目录中的缓存
Get-ChildItem -Path $targetDir -Include "__pycache__" -Recurse | ForEach-Object {
    Remove-Item $_.FullName -Recurse -Force
    Write-Host "✓ 删除: $($_.FullName)"
}
```

#### Linux/macOS Bash

```bash
TARGET_DIR="$HOME/.neko/plugins/rvc_singer"

echo "删除旧缓存文件..."
find "$TARGET_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
echo "✓ 缓存已清理"
```

### 第 4 步：配置修改

打开 `$TARGET_DIR/plugin.toml` 文件，修改 `[settings]` 部分：

```toml
[settings]
# RVC Studio 运行的主机地址
# 本地运行通常是 127.0.0.1，远程可以改为服务器 IP
rvc_studio_host = "127.0.0.1"

# RVC Studio 监听的端口
# 默认 19877（根据实际配置修改）
rvc_studio_port = 19877

# RVC 引擎根目录（必须是绝对路径！）
# Windows 示例：E:/path/to/RVC1006Nvidia
# Linux 示例：/home/user/rvc/RVC1006Nvidia
rvc_root_path = "<RVC_ROOT_DIR>"

# 默认使用的音色模型名称
# 必须与 RVC Studio 中已训练的模型名完全一致
default_model = "mi-test"

# 是否自动混合原歌曲的背景音乐
# true = 启用，false = 禁用
auto_mix_background = true
```

**⚠️ 关键配置**:

| 配置项 | 说明 | 示例 |
|--------|------|------|
| **rvc_root_path** | ⚠️ 必须是绝对路径，不能有相对路径 | ✅ `<RVC_ROOT_DIR>`（如 E:/rvc/1/RVC1006Nvidia） |
| **rvc_studio_port** | 与 RVC Studio 后端监听端口一致 | 19877 或根据实际 |
| **default_model** | 模型名必须存在于 RVC Studio | 查看 RVC Studio 面板 |

### 第 5 步：启动服务

#### 启动 RVC Studio

```powershell
# Windows 示例（替换为你的 RVC 根目录路径）
cd "<RVC_ROOT_DIR>"

# 如果有 GUI 程序，直接运行
.\RVCStudio.exe

# 或者运行 Python 后端（根据实际配置）
python -m rvc_studio.server
```

**验证 RVC Studio 是否在线**:

```powershell
# 测试 API 连接
$response = Invoke-WebRequest -Uri "http://127.0.0.1:19877/api/health" -ErrorAction SilentlyContinue
if ($response.StatusCode -eq 200) {
    Write-Host "✓ RVC Studio 在线"
} else {
    Write-Host "✗ RVC Studio 未响应"
}
```

#### 重启 NEKO 主程序

```powershell
# 完全关闭 NEKO（包括所有子进程）
Get-Process | Where-Object { $_.ProcessName -like "*neko*" } | Stop-Process -Force

# 等待 3 秒
Start-Sleep -Seconds 3

# 重启 NEKO
# Windows: 双击 N.E.K.O.exe
# 或从命令行启动：
& "C:\Path\To\NEKO.exe"
```

### 第 6 步：验证部署

#### 打开 NEKO 插件面板

1. 启动 NEKO 主程序
2. 打开界面（通常是浏览器访问 http://localhost:48911）
3. 导航到"插件管理"或"设置"
4. 查找"RVC歌声合成"插件

#### 检查插件状态

| 指标 | 预期值 | 故障排除 |
|------|--------|---------|
| **状态** | 已启用 ✅ | 如果禁用，检查 __pycache__ 和 BOM |
| **版本** | 0.1.0 | 检查 plugin.toml 中的 version |
| **UI 面板** | 可打开 | 检查 ui/panel.tsx 是否有语法错误 |

#### 功能测试

1. **打开 RVC 歌声合成面板**
   - 点击插件的"打开"或"设置"按钮
   - 应显示演唱请求表单

2. **检查 Studio 连接**
   - 查看顶部状态卡片
   - "Studio 状态"应显示"在线"（绿色脉动）
   - 如果显示"离线"，检查 RVC Studio 是否启动

3. **查询歌曲库**
   - 切换到"歌曲库"标签页
   - 应显示已有歌曲列表
   - 如果为空，使用"搜索下载"功能

4. **发送测试请求**
   - 输入歌曲名称（例如："晴天"）
   - 选择默认模型（通常已预设）
   - 点击"开始演唱"
   - 应显示进度条，最后返回成功消息

---

## 🔍 故障排除

### 问题 1：插件不显示

**症状**: NEKO 插件列表中找不到"RVC歌声合成"

**原因检查**:
```powershell
# 1. 检查文件是否存在
$targetDir = "$env:LOCALAPPDATA\N.E.K.O\plugins\rvc_singer"
Test-Path "$targetDir\__init__.py"        # 应返回 True
Test-Path "$targetDir\plugin.toml"        # 应返回 True
Test-Path "$targetDir\ui\panel.tsx"       # 应返回 True

# 2. 检查 BOM（特别是 __init__.py）
$content = Get-Content "$targetDir\__init__.py" -Encoding Byte | Select-Object -First 3
if ($content -eq @(239, 187, 191)) {
    Write-Host "✗ 文件有 BOM，需要移除"
} else {
    Write-Host "✓ BOM 正常"
}

# 3. 检查是否有 Python 语法错误
python -m py_compile "$targetDir\__init__.py"
```

**解决方案**:
1. 删除 __pycache__：`Remove-Item "$targetDir\__pycache__" -Recurse -Force`
2. 移除 BOM：使用 VS Code 打开 __init__.py → 右下角改为 "UTF-8 without BOM"
3. 重启 NEKO

### 问题 2：UI 面板无法打开

**症状**: 点击插件后没有反应或显示空白

**原因检查**:
```bash
# 查看浏览器控制台 (F12 → Console)
# 应显示 TypeScript 编译或加载错误

# 检查 panel.tsx 是否有语法错误
grep -n "export default" ui/panel.tsx  # 应有输出
```

**解决方案**:
1. 打开 VS Code，检查 panel.tsx 是否有红色波浪线
2. 查看浏览器控制台错误信息
3. 特别检查：imports、类型定义、React hooks 调用

### 问题 3：Studio 显示离线

**症状**: UI 面板中"Studio 状态"显示离线（红色）

**原因检查**:
```powershell
# 1. RVC Studio 是否在运行
Get-Process | Where-Object { $_.ProcessName -like "*rvc*" -or $_.ProcessName -like "*studio*" }

# 2. 端口是否监听
netstat -ano | findstr "19877"

# 3. 健康检查 API
Invoke-WebRequest -Uri "http://127.0.0.1:19877/api/health" -Verbose
```

**解决方案**:
1. 启动 RVC Studio 独立程序
2. 检查配置中的主机地址和端口是否正确
3. 检查防火墙是否阻止了连接
4. 在浏览器中访问 http://127.0.0.1:19877/api/health 测试

### 问题 4：演唱请求失败

**症状**: 点击"开始演唱"后显示错误消息

**错误消息说明**:

| 错误 | 原因 | 解决方案 |
|------|------|---------|
| "RVC Studio 未连接" | Studio 离线 | 启动 RVC Studio |
| "歌曲不存在" | 本地没有歌曲文件 | 使用"搜索下载" |
| "模型不存在" | 模型名拼写错误 | 检查 plugin.toml 的 default_model |
| "参数验证失败" | 输入格式不正确 | 检查歌曲名称是否为空 |
| "服务器错误" | RVC Studio 处理异常 | 查看 RVC Studio 日志 |

**查看后端日志**:
```powershell
# Windows
$logDir = "$env:LOCALAPPDATA\N.E.K.O\logs"
Get-Content "$logDir\rvc_singer.log" -Tail 50

# Linux
tail -50 ~/.neko/logs/rvc_singer.log
```

### 问题 5：表单保存失败

**症状**: 修改配置后点击"保存配置"无反应

**原因检查**:
1. 后端 update_config 入口是否正确定义
2. 权限设置是否允许 action:call
3. 参数验证是否通过

**解决方案**:
```python
# 检查 __init__.py 中的 update_config_entry 方法
# 确保：
# 1. 有 @ui.action 装饰
# 2. 有 @plugin_entry 装饰
# 3. 返回 Ok({...}) 或 Err(SdkError(...))
```

---

## ✅ 验证清单 (Post-Deployment)

### 基础验证

- [ ] 插件在 NEKO 插件列表中出现
- [ ] 插件状态显示"已启用"
- [ ] 可以打开插件 UI 面板
- [ ] 没有控制台错误 (F12)
- [ ] Studio 状态显示"在线"

### 功能验证

- [ ] 可以查看歌曲库
- [ ] 可以查看可用模型
- [ ] 可以输入歌曲名称
- [ ] 可以调整变调值
- [ ] 可以提交演唱请求
- [ ] 进度条实时更新
- [ ] 完成后显示成功消息

### 配置验证

- [ ] 可以修改 Studio 主机地址
- [ ] 可以修改 Studio 端口
- [ ] 可以修改默认模型
- [ ] 可以修改自动混音设置
- [ ] 保存配置后提示重启

---

## 🎓 最佳实践

### 日常维护

```powershell
# 定期检查插件状态
# 每次启动 NEKO 前确保 RVC Studio 已启动

# 如果修改了代码或配置：
# 1. 同步文件
# 2. 删除 __pycache__
# 3. 重启 NEKO
```

### 性能优化

```python
# 后端：使用连接池复用
# 缓存机制已内置（5分钟TTL）
# 并发控制已实现（防止重复提交）

# 前端：使用 React.useState 缓存
# useEffect 依赖精确指定
# 条件渲染避免无用 DOM
```

### 安全建议

```toml
# plugin.toml 中的 permissions 最小化
permissions = ["state:read", "config:read", "action:call"]

# 后端参数验证
if not isinstance(port, int) or not (1 <= port <= 65535):
    return Err(SdkError("端口无效"))
```

---

## 📚 参考文档

- **NEKO 官方**: https://project-neko.online/zh-CN/
- **RVC Studio**: https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI
- **本项目文档**:
  - `B_ENDPOINT_UPGRADE_REPORT.md` - UI 升级详情
  - `NEKO_COMPLIANCE_CHECKLIST.md` - 规范检查清单

---

## 📞 技术支持

### 常见问题 FAQ

**Q: 能在远程服务器上运行吗?**  
A: 可以。修改 plugin.toml 的 `rvc_studio_host` 为服务器 IP 地址即可。

**Q: 支持多个 RVC Studio 实例吗?**  
A: 目前只支持一个实例。可通过配置切换不同的 Studio 地址。

**Q: 歌曲文件在哪里?**  
A: 由 RVC Studio 管理。通常在 RVC 根目录的 `songs/` 或 `datasets/` 目录。

**Q: 如何添加自己的模型?**  
A: 在 RVC Studio 中训练或导入模型，然后在 plugin.toml 中指定模型名称。

---

**状态**: ✅ 部署指南完成  
**版本**: 2.0 Professional Edition  
**最后更新**: 2026-07-17
