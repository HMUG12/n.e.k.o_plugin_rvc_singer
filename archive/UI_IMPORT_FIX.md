# 🔧 RVC Singer 插件 B 端 UI 导入问题修复

## 问题诊断

**错误信息**：`Bare import 'react' cannot resolve inside the surface iframe`

**根本原因**：违反了 **NEKO Hosted UI 沙箱隔离规则**

### 不允许的导入
```tsx
❌ import { CSSProperties } from "react"
❌ import React from "react"
```

Hosted UI iframe 是隔离的沙箱环境，**禁止直接导入 npm 包**（react、lodash 等）。所有外部依赖必须通过 `@neko/plugin-ui` 的重新导出来获取。

### 官方规则（NEKO UI 设计限制）

| 允许 ✅ | 禁止 ❌ |
|--------|--------|
| `import { ... } from "@neko/plugin-ui"` | `import ... from "react"` |
| `import ... from "./relative/path"` | `import ... from "lodash"` |
| `import type { ... } from "@neko/plugin-ui"` | `import ... from "npm-package"` |

---

## 🔨 修复内容

### 修改文件
📝 `ui/panel.tsx`（第 1-30 行）

### 修复步骤

#### ✅ 步骤 1：从 `@neko/plugin-ui` 导入 React 工具

```tsx
// 旧代码 ❌
import {
  Page, Card, Grid, Stack, Text, ... 
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"
import { CSSProperties } from "react"        // ❌ 裸导入
import React from "react"                    // ❌ 裸导入

// 新代码 ✅
import {
  Page, Card, Grid, Stack, Text,
  useForm, useEffect, useToast, useConfirm,
  useLocalState, useState,
  React,  // ← 从 @neko/plugin-ui 导出
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type CSSProperties = React.CSSProperties  // ← 类型别名
```

#### ✅ 步骤 2：替换所有 `React.useState` 为 `useState`

```tsx
// 旧代码 ❌
const [activeTab, setActiveTab] = React.useState<"sing" | "config" | "library">("sing")

// 新代码 ✅
const [activeTab, setActiveTab] = useState<"sing" | "config" | "library">("sing")
```

---

## 📋 检查清单

- [x] 移除所有 `import ... from "react"` 裸导入
- [x] 移除所有 `import ... from "npm-package"` 裸导入
- [x] 所有 React/Hook 工具都从 `@neko/plugin-ui` 导入
- [x] 替换 `React.useState` → `useState`
- [x] 类型别名使用 `React.CSSProperties`（通过 @neko/plugin-ui 的 React）
- [x] 代码 Lint 检查通过（0 错误）

---

## 🎯 NEKO Hosted UI 最佳实践

### 可用的导入源

```tsx
// ✅ 官方 UI 库
import { Page, Card, Stack, Button, ... } from "@neko/plugin-ui"

// ✅ 官方 Hook
import { useForm, useToast, useAsync, useState, useEffect } from "@neko/plugin-ui"

// ✅ React 基础（通过 @neko/plugin-ui 重新导出）
import { React, useState, useEffect, useRef } from "@neko/plugin-ui"

// ✅ 类型定义
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

// ✅ 相对路径导入（本地 helper）
import { formatDate } from "./helpers"
```

### 不支持的特性

```tsx
// ❌ 禁止：npm 包导入
import lodash from "lodash"
import axios from "axios"

// ❌ 禁止：Class 组件
class MyComponent extends React.Component { ... }

// ❌ 禁止：React Context
import { createContext, useContext } from "react"

// ❌ 禁止：Portal API
import { createPortal } from "react-dom"

// ❌ 禁止：dangerouslySetInnerHTML
<div dangerouslySetInnerHTML={{__html: html}} />
```

---

## ✨ 修复后的代码示例

```tsx
import {
  Page, Card, Stack, Button, Input, Field,
  useForm, useEffect, useToast,
  useState,
  React,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type CSSProperties = React.CSSProperties

export default function MyPanel(props: PluginSurfaceProps<any>) {
  const [count, setCount] = useState(0)  // ✅ 正确
  const toast = useToast()               // ✅ 正确
  
  return (
    <Page title="我的面板">
      <Button onClick={() => setCount(count + 1)}>
        点击次数: {count}
      </Button>
    </Page>
  )
}
```

---

## 🚀 验证步骤

1. **同步代码到部署目录**
   ```bash
   cp -r ./ui/panel.tsx <NEKO_PLUGINS_DIR>/rvc_singer/ui/
   ```

2. **删除缓存**
   ```bash
   rm -rf <NEKO_PLUGINS_DIR>/rvc_singer/__pycache__/
   ```

3. **重启 N.E.K.O**
   - 关闭 NEKO 主程序
   - 删除浏览器缓存（DevTools → Application → Clear Site Data）
   - 重新打开 NEKO

4. **验证 UI 加载**
   - 打开 RVC Singer 插件面板
   - 检查 Console（F12）是否有错误
   - 验证标签页、表单、按钮是否正常工作

---

## 📚 参考资源

- **官方文档**：[UI 设置面板开发](https://project-neko.online/zh-CN/guide/plugin-dev/ui-settings.html)
- **NEKO Skill**：`@skill://neko-plugin-dev` → 参考篇 → `03-ui-settings.md`
- **可用组件列表**：17 个官方 UI 组件（已验证）
- **可用 Hooks**：8 个 React Hook + 自定义 Hook

---

## ✅ 修复结果

| 指标 | 状态 |
|------|------|
| 代码 Lint | ✅ 0 错误 |
| 导入规范 | ✅ 100% 合规 |
| 沙箱隔离 | ✅ 遵守 |
| 类型检查 | ✅ 通过 |

**现在可以安全地在 NEKO 中加载 B 端 UI 面板！** 🎉
