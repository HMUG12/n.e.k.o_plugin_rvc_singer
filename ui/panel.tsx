import {
  Page,
  Card,
  Grid,
  Stack,
  Text,
  Button,
  StatusBadge,
  KeyValue,
  ButtonGroup,
  Progress,
  Toolbar,
  RefreshButton,
  useEffect,
  useLocalState,
  useToast,
  useState,
  React,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

// ═══════════════ 类型 ═══════════════

type RvcPanelState = {
  // 连接
  studio_available?: boolean
  studio_url?: string
  // 任务
  active_task?: string | null
  song_name?: string
  model?: string
  progress?: number
  step?: string
  // 完成结果
  message?: string
  merged_audio_url?: string
  duration_seconds?: number
  lyrics_preview?: string
  lyrics_source?: string       // provided / lrc / asr / placeholder
  asr_quality?: string         // "ok" / "degraded"
  // A/B 对比
  compare_mode?: boolean
  compare_results?: Array<{
    model?: string
    url?: string          // 向后兼容：旧格式使用 url
    audio_url?: string    // 新格式标准字段
    ok?: boolean
    error?: string
  }>
}

// ═══════════════ 工具函数 ═══════════════

function fmtDuration(seconds?: number): string {
  if (!seconds || seconds <= 0) return "—"
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}分${s}秒`
}

function truncate(text?: string, maxLen = 280): string {
  if (!text) return ""
  return text.length > maxLen ? text.slice(0, maxLen) + "…" : text
}

// ═══════════════ 主组件 ═══════════════

export default function RvcSingerPanel(props: PluginSurfaceProps<RvcPanelState>) {
  const { state = {}, actions = [], api } = props
  const { toast } = useToast()
  const [reconnecting, setReconnecting] = useState(false)
  const [cancelling, setCancelling] = useState(false)

  // ── action 缓存（首次渲染时 actions 可能未就绪） ──
  const [cachedActions, setCachedActions] = useLocalState<HostedAction[]>(
    "ca",
    () => [],
  )
  useEffect(() => {
    if (actions.length > 0 && actions.length !== cachedActions.length) {
      setCachedActions(actions)
    }
  }, [actions, cachedActions.length, setCachedActions])
  const effectiveActions = cachedActions.length > 0 ? cachedActions : actions

  // ── 派生状态 ──
  const isOnline = state.studio_available === true
  const isProcessing = !!(state.active_task && state.active_task.length > 0)
  const isCompleted = !isProcessing && !!state.merged_audio_url
  const isCompare = !isProcessing && state.compare_mode === true
  const isIdle = !isProcessing && !isCompleted && !isCompare

  const statusVariant: "success" | "warning" | "error" = isProcessing
    ? "warning"
    : isOnline
      ? "success"
      : "error"
  const statusLabel = isProcessing ? "处理中" : isOnline ? "就绪" : "离线"

  // ── 匹配 action（支持多种 NEKO 命名格式：id / entry_id / plugin.id） ──
  function findAction(actionsArr: HostedAction[], name: string): HostedAction | undefined {
    const candidates = [name, `rvc_singer.${name}`, `rvc_singer:${name}`]
    for (const c of candidates) {
      const found = actionsArr.find((a) => a.id === c || a.entry_id === c)
      if (found) return found
    }
    return undefined
  }

  // ── 调用 action 并提取 payload ──
  async function callAction(
    action: HostedAction,
    params: Record<string, unknown> = {},
  ): Promise<any> {
    if (!api || typeof api.call !== "function") {
      throw new Error("面板 API 未就绪，请刷新面板")
    }
    const raw: any = await api.call(action.entry_id || action.id, params)
    // NEKO api.call: {ok:true, value:{...}} | {result:...} | 直接返回
    return raw?.value ?? raw?.result ?? raw?.data ?? raw
  }

  // ── 重连 ──
  const handleReconnect = async () => {
    const action = findAction(effectiveActions, "reconnect_studio")
    if (!action) {
      toast({
        type: "error",
        message:
          "重连功能未加载，请重启 NEKO。" +
          (effectiveActions.length > 0
            ? ` 已有 actions: ${effectiveActions.map((a) => a.id || a.entry_id).join(", ")}`
            : " actions 列表为空（插件可能尚未初始化完成）"),
      })
      return
    }
    setReconnecting(true)
    try {
      const payload: any = await callAction(action, {})
      // 后端返回 Ok({"online": True, ...})，兼容 NEKO 不同版本 api.call 的解包格式
      const ok =
        payload?.online === true ||
        (typeof payload === "object" && "ok" in payload && (payload as any).ok === true)
      toast({
        type: ok ? "success" : "warning",
        message: ok
          ? "已连接到 RVC Studio"
          : `Studio 未响应${payload?.message ? ": " + payload.message : ""}`,
      })
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error)
      toast({ type: "error", message: `重连失败: ${msg}` })
    } finally {
      setReconnecting(false)
    }
  }

  // ── 取消当前任务 ──
  const handleCancel = async () => {
    const action = findAction(effectiveActions, "cancel_song")
    if (!action) {
      toast({ type: "error", message: "取消功能未加载，请重启 NEKO" })
      return
    }
    setCancelling(true)
    try {
      const payload: any = await callAction(action, {})
      toast({
        type: "success",
        message: payload?.message || "任务已取消",
      })
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error)
      toast({ type: "error", message: `取消失败: ${msg}` })
    } finally {
      setCancelling(false)
    }
  }

  // ═══════════════ 渲染 ═══════════════

  return (
    <Page title="RVC 歌声合成">
      {/* ── 顶部工具栏 ── */}
      <Toolbar>
        <RefreshButton onClick={() => api?.refresh?.()} />
      </Toolbar>

      <Stack direction="col" gap="16px">
        {/* ── 连接状态卡 ── */}
        <Card>
          <Stack direction="col" gap="10px">
            <Stack direction="row" align="center" gap="8px">
              <Text weight="semibold">连接状态</Text>
              <StatusBadge variant={statusVariant}>{statusLabel}</StatusBadge>
            </Stack>

            <KeyValue
              label="Studio 地址"
              value={state.studio_url || "未配置"}
            />
            {state.model && (
              <KeyValue label="当前模型" value={state.model} />
            )}
          </Stack>
        </Card>

        {/* ── 处理中卡 ── */}
        {isProcessing && (
          <Card>
            <Stack direction="col" gap="12px">
              <Stack direction="row" align="center" gap="8px">
                <Text weight="semibold">当前任务</Text>
                <StatusBadge variant="warning">进行中</StatusBadge>
              </Stack>

              <KeyValue label="歌曲" value={state.song_name || "—"} />
              {state.model && (
                <KeyValue label="模型" value={state.model} />
              )}
              <KeyValue label="步骤" value={state.step || "—"} />

              <Progress value={Math.min(state.progress || 0, 100)} max={100} />
              <Text size="sm" color="secondary">
                {state.progress || 0}%
              </Text>

              <Button
                onClick={handleCancel}
                disabled={cancelling}
                variant="danger"
                size="small"
              >
                {cancelling ? "取消中..." : "⏹ 取消任务"}
              </Button>
            </Stack>
          </Card>
        )}

        {/* ── 完成卡（演唱完成） ── */}
        {isCompleted && (
          <Card>
            <Stack direction="col" gap="12px">
              <Stack direction="row" align="center" gap="8px">
                <Text weight="semibold">最近完成</Text>
                <StatusBadge variant="success">完成</StatusBadge>
                {state.asr_quality === "degraded" && (
                  <StatusBadge variant="warning">歌词降级</StatusBadge>
                )}
              </Stack>

              <Grid columns={2} gap="8px">
                <KeyValue label="歌曲" value={state.song_name || "—"} />
                {state.model && (
                  <KeyValue label="模型" value={state.model} />
                )}
                <KeyValue
                  label="时长"
                  value={fmtDuration(state.duration_seconds)}
                />
                <KeyValue label="格式" value="MP3" />
              </Grid>

              {state.asr_quality === "degraded" && (
                <Card
                  style={{
                    padding: "8px 10px",
                    backgroundColor: "var(--neko-warning-bg, #fff3e0)",
                  }}
                >
                  <Text size="sm" color="secondary">
                    ⚠️ B 端缺少 CUDA 库（cublas64_12.dll），ASR 歌词转录降级。
                    <br />
                    角色口型将无法精准同步，歌词仅显示歌名。
                  </Text>
                </Card>
              )}

              {state.lyrics_preview && (
                <Card
                  style={{
                    padding: "8px 10px",
                    backgroundColor: "var(--neko-surface-variant, #f5f5f5)",
                  }}
                >
                  <Text size="sm" color="secondary" style={{ whiteSpace: "pre-wrap" }}>
                    {truncate(state.lyrics_preview)}
                  </Text>
                </Card>
              )}
            </Stack>
          </Card>
        )}

        {/* ── A/B 对比完成卡 ── */}
        {isCompare && state.compare_results && state.compare_results.length > 0 && (
          <Card>
            <Stack direction="col" gap="10px">
              <Stack direction="row" align="center" gap="8px">
                <Text weight="semibold">A/B 对比</Text>
                <StatusBadge variant="success">完成</StatusBadge>
              </Stack>

              <Text size="sm" color="secondary">
                歌曲：{state.song_name || "—"}
              </Text>

              <Stack direction="col" gap="4px">
                {state.compare_results.map((r, i) => {
                  const ok = r.ok ?? (!!r.url || !!r.audio_url);
                  return (
                    <KeyValue
                      key={i}
                      label={`${r.model || `模型${i + 1}`}`}
                      value={ok ? "✓ 试听中" : r.error || "失败"}
                    />
                  );
                })}
              </Stack>
            </Stack>
          </Card>
        )}

        {/* ── 空闲提示 ── */}
        {isIdle && (
          <Card>
            <Stack direction="col" gap="6px">
              <Text weight="semibold">提示</Text>
              <Text size="sm" color="secondary">
                在对话中说「唱首歌」或「帮我找一首歌」即可开始使用喵～
              </Text>
            </Stack>
          </Card>
        )}

        {/* ── 操作卡 ── */}
        <Card>
          <Stack direction="col" gap="8px">
            <Text weight="semibold">操作</Text>
            <ButtonGroup>
              <Button
                onClick={handleReconnect}
                disabled={reconnecting}
                variant="primary"
              >
                {reconnecting ? "连接中..." : "重新连接"}
              </Button>
            </ButtonGroup>
          </Stack>
        </Card>
      </Stack>
    </Page>
  )
}
