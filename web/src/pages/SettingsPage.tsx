import { useEffect, useState } from "react"
import { toast } from "sonner"
import {
  api,
  type AgentConfig,
  type ExportPreviewResp,
  type GraduationResp,
  type GpuInfo,
  type NotifyConfig,
  type ProbeResult,
  type ReportResp,
  type RunLogResp,
  type RunStatus,
} from "@/lib/api"
import { useCohort } from "@/lib/cohort"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"

export default function SettingsPage() {
  return (
    <div className="space-y-4">
      <RunPanel />
      <DeliveryPanel />
      <ApiConfigPanel />
      <NotifyPanel />
      <ReportPanel />
    </div>
  )
}

// ------------------------------------------------------------------
// 运行控制
// ------------------------------------------------------------------

function RunPanel() {
  const [trials, setTrials] = useState("2")
  const [wakeEvery, setWakeEvery] = useState("5")
  const [workers, setWorkers] = useState("")
  const [hours, setHours] = useState("")
  const [noAgent, setNoAgent] = useState(false)
  const [newCohort, setNewCohort] = useState(false)
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState(false)
  const [gpuList, setGpuList] = useState<GpuInfo[] | null>(null)
  const [selGpus, setSelGpus] = useState<number[]>([])

  // 本机 GPU 清单：无 GPU（nvidia-smi 缺失/驱动异常）→ 空数组，选卡区整体隐藏
  useEffect(() => {
    api.gpusList().then((r) => setGpuList(r.gpus)).catch(() => setGpuList([]))
  }, [])

  const toggleGpu = (idx: number) => {
    setSelGpus((cur) => cur.includes(idx)
      ? cur.filter((g) => g !== idx) : [...cur, idx].sort((a, b) => a - b))
  }

  const { data: log, error } = usePolling<RunLogResp>(
    () => api.runLog(300),
    2000,
  )
  const running = log?.running ?? false

  const start = async () => {
    setBusy(true)
    try {
      const t = parseInt(trials)
      const w = parseInt(wakeEvery)
      const k = parseInt(workers)
      const h = parseFloat(hours)
      await api.runStart({
        trials: Number.isFinite(t) && t > 0 ? t : undefined,
        wake_every: Number.isFinite(w) && w > 0 ? w : undefined,
        workers: Number.isFinite(k) && k > 0 ? k : undefined,
        max_duration_h: Number.isFinite(h) && h > 0 ? h : undefined,
        no_agent: noAgent,
        new_cohort: newCohort,
        note: note.trim() || undefined,
        gpus: selGpus.length > 0 ? selGpus : undefined,
      })
      toast.success("搜索已启动")
    } catch (e) {
      toast.error(`启动失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  const stop = async () => {
    setBusy(true)
    try {
      const r = await api.runStop()
      toast.success(
        r.marked_failed?.length
          ? `已停止（进行中的 trial#${r.marked_failed.join(", #")} 标记为失败）`
          : "已停止",
      )
    } catch (e) {
      toast.error(`停止失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          运行控制
          {running ? (
            <Badge className="bg-emerald-600/15 text-emerald-700 dark:text-emerald-400">
              运行中{log?.pid ? `（pid ${log.pid}）` : ""}
            </Badge>
          ) : log?.started_at ? (
            <Badge variant="secondary">
              上次运行 {log.started_at}
              {log.exit_code === 0 ? "（正常结束）" : log.stopped ? "（手动停止）" : `（退出码 ${log.exit_code}）`}
            </Badge>
          ) : (
            <Badge variant="secondary">空闲</Badge>
          )}
        </CardTitle>
        <CardDescription>
          启动一次搜索（子进程方式，等价于 python cli.py run）。记录按分区管理、永不删除：
          训练代码或优化目标变化时会自动新开分区；也可手动「新开分区」并写备注。
          并发数留空取 settings 默认；时长上限到点后在途试验跑完即优雅收尾。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-4">
          <div className="space-y-1">
            <Label htmlFor="trials">本次试验数</Label>
            <Input id="trials" className="w-28" value={trials}
                   onChange={(e) => setTrials(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="wake">每 N 次唤醒 agent</Label>
            <Input id="wake" className="w-28" value={wakeEvery}
                   onChange={(e) => setWakeEvery(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="workers">并发数</Label>
            <Input id="workers" className="w-24" placeholder="默认" value={workers}
                   onChange={(e) => setWorkers(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="hours">时长上限（小时）</Label>
            <Input id="hours" className="w-24" placeholder="不限" value={hours}
                   onChange={(e) => setHours(e.target.value)} />
          </div>
          <div className="flex items-center gap-2 pb-2">
            <Switch id="noagent" checked={noAgent} onCheckedChange={setNoAgent} />
            <Label htmlFor="noagent">不用 agent（纯 Optuna）</Label>
          </div>
          <div className="flex items-center gap-2 pb-2">
            <Switch id="newcohort" checked={newCohort} onCheckedChange={setNewCohort} />
            <Label htmlFor="newcohort">新开分区（不删除历史记录）</Label>
          </div>
          <div className="space-y-1">
            <Label htmlFor="note">分区备注</Label>
            <Input id="note" className="w-44" placeholder="如：换了新模型" value={note}
                   onChange={(e) => setNote(e.target.value)} />
          </div>
          <div className="ml-auto flex gap-2">
            <Button onClick={start} disabled={running || busy}>开始搜索</Button>
            <Button variant="destructive" onClick={stop} disabled={!running || busy}>停止</Button>
          </div>
        </div>

        {(gpuList?.length ?? 0) > 0 && (
          <div className="space-y-1.5">
            <Label>
              GPU 选择（注入 CUDA_VISIBLE_DEVICES，成本按所选卡数折算 GPU·小时）
            </Label>
            <div className="flex flex-wrap gap-2">
              {gpuList!.map((g) => {
                const active = selGpus.includes(g.index)
                const freeMb = g.memory_total_mb - g.memory_used_mb
                return (
                  <button key={g.index} type="button" onClick={() => toggleGpu(g.index)}
                          className={`rounded-md border px-2.5 py-1.5 text-left text-xs transition-colors ${
                            active
                              ? "border-emerald-500 bg-emerald-50 dark:bg-emerald-950/40"
                              : "hover:bg-muted/60"
                          }`}>
                    <div className="font-medium">
                      {active ? "✓ " : ""}GPU {g.index} · {g.name}
                    </div>
                    <div className="text-muted-foreground mt-0.5">
                      显存 {Math.round(freeMb / 1024 * 10) / 10}/{Math.round(g.memory_total_mb / 1024 * 10) / 10} GB 空闲
                      · 利用率 {g.utilization}%
                    </div>
                  </button>
                )
              })}
            </div>
            <p className="text-muted-foreground text-xs">
              不选 = 不限制（训练脚本按自身默认用卡）；选中后成本统计按卡数折算。
            </p>
          </div>
        )}

        {error && <div className="text-red-600 text-sm">状态加载失败：{error}</div>}

        <div>
          <div className="text-muted-foreground mb-1 text-xs">
            实时日志{log?.log_path ? `（${log.log_path}）` : ""}
          </div>
          <ScrollArea className="h-64 rounded-md border bg-neutral-950 p-3">
            <pre className="font-mono text-xs leading-relaxed whitespace-pre-wrap text-neutral-200">
              {log?.text || (running ? "等待输出…" : "（暂无运行日志）")}
            </pre>
          </ScrollArea>
        </div>
      </CardContent>
    </Card>
  )
}

// ------------------------------------------------------------------
// 成果交付：毕业赛（best 全量复验）+ 配置回写（best 合并进用户配置文件）
// ------------------------------------------------------------------

function DeliveryPanel() {
  const cohort = useCohort()
  const { data: runStatus } = usePolling<RunStatus>(api.runStatus, 3000)
  const { data: grad } = usePolling<GraduationResp>(() => api.graduateResult(cohort), 5000)
  const [starting, setStarting] = useState(false)
  const running = runStatus?.running ?? false

  const startGraduation = async () => {
    setStarting(true)
    try {
      await api.graduateStart()
      toast.success("毕业赛已开始：best 配置全量数据 + 满轮数复验（进度看下方实时日志）")
    } catch (e) {
      toast.error(`启动失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setStarting(false)
    }
  }

  const g = grad?.result
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">成果交付</CardTitle>
        <CardDescription>
          搜索结束后的两步收尾：①「毕业赛」把最优配置在全量数据、满训练轮数下复验一次
          （隔离运行，不污染搜索记录）；②「配置回写」把最优超参数合并进你自己的配置文件。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <span className="font-medium text-sm">① 最优配置毕业赛</span>
            {g?.status === "ok" && (
              <Badge className={g.verdict === "pass"
                ? "bg-emerald-600/15 text-emerald-700 dark:text-emerald-400"
                : "bg-amber-600/15 text-amber-700 dark:text-amber-400"}>
                {g.verdict === "pass" ? "✔ 全量复验通过" : "✘ 全量下回落"}
              </Badge>
            )}
            {g?.status === "failed" && <Badge variant="destructive">复验失败</Badge>}
            <Button size="sm" variant="outline" onClick={startGraduation}
                    disabled={running || starting}>
              {running ? "运行槽被占用" : starting ? "启动中…" : "举办毕业赛"}
            </Button>
            {grad?.updated && (
              <span className="text-muted-foreground text-xs">上次：{grad.updated}</span>
            )}
          </div>
          {g && (
            <div className="text-muted-foreground space-y-1 rounded-md border p-3 text-sm">
              {g.status === "ok" ? (
                <>
                  <div>
                    trial#{g.best_trial} 的 best 配置：
                    <span className="text-foreground font-mono"> {g.primary} </span>
                    搜索期 <span className="text-foreground font-mono">{Number(g.best_value.toPrecision(6))}</span>
                    → 全量复验 <span className="text-foreground font-mono">{Number((g.value ?? 0).toPrecision(6))}</span>
                    （Δ={Number((g.delta ?? 0).toPrecision(4))}，耗时 {g.duration_s}s）
                  </div>
                  {g.iter_param && (
                    <div className="text-xs">
                      训练轮数已拉满：{g.iter_param.name} {String(g.iter_param.before)} → {String(g.iter_param.after)}；
                      数据比例强制 100%
                    </div>
                  )}
                  {g.verdict === "regressed" && (
                    <div className="text-amber-600 text-xs">
                      全量数据下明显回落——搜索期结果可能受益于抽样数据，建议检查数据分布或以此配置继续微调。
                    </div>
                  )}
                </>
              ) : (
                <div>
                  {g.reason || g.status}
                  {g.hint && <span className="text-xs"> ｜ 建议：{g.hint}</span>}
                </div>
              )}
            </div>
          )}
        </div>

        <ExportSection />
      </CardContent>
    </Card>
  )
}

function ExportSection() {
  const [target, setTarget] = useState("")
  const [preview, setPreview] = useState<ExportPreviewResp | null>(null)
  const [busy, setBusy] = useState(false)

  const doPreview = async () => {
    if (!target.trim()) {
      toast.error("请先填写目标配置文件路径")
      return
    }
    setBusy(true)
    try {
      setPreview(await api.exportPreview(target.trim()))
    } catch (e) {
      setPreview(null)
      toast.error(`预览失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  const doApply = async () => {
    setBusy(true)
    try {
      const r = await api.exportApply(target.trim())
      toast.success(r.summary)
      setPreview(null)
    } catch (e) {
      toast.error(`回写失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  const n = preview ? preview.changed.length + preview.appended.length : 0
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="font-medium text-sm">② 配置回写</span>
        <Input className="max-w-md" placeholder="目标配置文件（相对项目目录或绝对路径，.yaml/.json）"
               value={target} onChange={(e) => { setTarget(e.target.value); setPreview(null) }} />
        <Button size="sm" variant="outline" onClick={doPreview} disabled={busy}>预览变更</Button>
        {preview && (
          <Button size="sm" onClick={doApply} disabled={busy}>
            确认写入（自动备份 .bak）
          </Button>
        )}
      </div>
      <p className="text-muted-foreground text-xs">
        把当前最优超参数合并进你的训练配置文件：顶层同名键覆盖、异名键追加；写入前原文件备份为
        &lt;文件名&gt;.bak。先「预览变更」确认无误再写入。
      </p>
      {preview && (
        <div className="space-y-1 rounded-md border p-3 text-sm">
          <div className="font-medium">
            trial#{preview.best_trial}（{Number(preview.best_value.toPrecision(6))}）→ {preview.target}
            ：共 {n} 项变更
          </div>
          {preview.changed.map((c) => (
            <div key={c.key} className="text-muted-foreground font-mono text-xs">
              ~ {c.key}: {JSON.stringify(c.old)} → <span className="text-foreground">{JSON.stringify(c.new)}</span>
            </div>
          ))}
          {preview.appended.map((a) => (
            <div key={a.key} className="text-muted-foreground font-mono text-xs">
              + {a.key}: <span className="text-foreground">{JSON.stringify(a.new)}</span>
            </div>
          ))}
          {n === 0 && <div className="text-muted-foreground text-xs">最优配置与目标文件已一致，无需写入。</div>}
        </div>
      )}
    </div>
  )
}

function ApiConfigPanel() {
  const { data: current, error, } = usePolling<AgentConfig>(api.agentConfig, 15000)
  const [model, setModel] = useState("")
  const [baseUrl, setBaseUrl] = useState("")
  const [token, setToken] = useState("")
  const [probing, setProbing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [probeResult, setProbeResult] = useState<ProbeResult | null>(null)

  const buildBody = () => ({
    model: model.trim() || undefined,
    base_url: baseUrl.trim() || undefined,
    auth_token: token.trim() || undefined,
  })

  const probe = async () => {
    setProbing(true)
    setProbeResult(null)
    try {
      setProbeResult(await api.probe(buildBody()))
    } catch (e) {
      toast.error(`探测请求失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setProbing(false)
    }
  }

  const save = async () => {
    setSaving(true)
    try {
      const r = await api.save(buildBody())
      toast.success(`已保存并验证通过（写回字段：${r.write_back.changed.join(", ") || "无变化"}）`)
      r.warnings.forEach((w) => toast.warning(w))
      setToken("")
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">大模型 API 配置</CardTitle>
        <CardDescription>
          切换 agent 使用的模型端点。保存前会先做两级探测（ping + tool-use），失败不落盘。
          留空的字段保持原值不变。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && <div className="text-red-600 text-sm">配置加载失败：{error}</div>}
        {current && (
          <div className="text-muted-foreground space-y-1 rounded-md border p-3 text-sm">
            <div>当前模型：<span className="text-foreground font-mono">{current.model}</span>
              {!current.enabled && <Badge variant="secondary" className="ml-2">agent 已禁用</Badge>}
            </div>
            <div>
              当前端点：<span className="text-foreground font-mono">{current.base_url || "（SDK 默认）"}</span>
              {current.env_refs.base_url && <Badge variant="outline" className="ml-2">${"${ENV:...}"} 引用</Badge>}
            </div>
            <div>
              当前凭据：<span className="text-foreground font-mono">{current.auth_token_masked}</span>
              <span className="ml-2">来源：{current.auth_token_source}</span>
            </div>
          </div>
        )}

        <div className="grid gap-3 md:grid-cols-3">
          <div className="space-y-1">
            <Label htmlFor="model">模型名</Label>
            <Input id="model" placeholder={current?.model || "如 qwen3-max"}
                   value={model} onChange={(e) => setModel(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="baseurl">端点 base_url</Label>
            <Input id="baseurl" placeholder="留空=保持现状"
                   value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="token">auth_token</Label>
            <Input id="token" type="password" placeholder="留空=保持现状"
                   value={token} onChange={(e) => setToken(e.target.value)} />
          </div>
        </div>
        {token.trim() && (
          <p className="text-amber-600 text-xs">
            ⚠ token 将以明文写入 demo/configs/settings.yaml（覆盖现有的环境变量引用），注意不要把含密钥的配置提交到公开仓库。
          </p>
        )}

        {probeResult && (
          <div className={`rounded-md border p-3 text-sm ${probeResult.ok ? "border-emerald-300 bg-emerald-50 dark:bg-emerald-950/30" : "border-red-300 bg-red-50 dark:bg-red-950/30"}`}>
            <Badge className={probeResult.ok ? "bg-emerald-600/15 text-emerald-700" : "bg-red-600/15 text-red-700"}>
              {probeResult.ok ? "探测通过" : `失败于 ${probeResult.stage}`}
            </Badge>
            <span className="ml-2">{probeResult.detail}</span>
          </div>
        )}

        <div className="flex gap-2">
          <Button variant="outline" onClick={probe} disabled={probing || saving}>
            {probing ? "探测中…" : "探测连通性"}
          </Button>
          <Button onClick={save} disabled={probing || saving}>
            {saving ? "探测并保存中…" : "探测并保存"}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// ------------------------------------------------------------------
// 通知配置（webhook：会话结束 / agent 降级推送）
// ------------------------------------------------------------------

const NOTIFY_FORMATS = [
  { value: "generic", label: "通用（{\"text\": …}）" },
  { value: "dingtalk", label: "钉钉自定义机器人" },
  { value: "lark", label: "飞书自定义机器人" },
  { value: "slack", label: "Slack incoming webhook" },
]
const NOTIFY_EVENT_META: Record<string, string> = {
  session_end: "会话结束（跑完 / 中断 / 预算耗尽）",
  agent_degrade: "agent 连续失败降级为无 agent 巡航",
}

function NotifyPanel() {
  const { data: current, error } = usePolling<NotifyConfig>(api.notifyConfig, 15000)
  const [url, setUrl] = useState("")
  const [format, setFormat] = useState("")
  const [events, setEvents] = useState<string[] | null>(null)
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)

  const effEvents = events ?? current?.events ?? []
  const effEnabled = enabled ?? current?.enabled ?? true

  const toggleEvent = (name: string) => {
    setEvents(effEvents.includes(name)
      ? effEvents.filter((e) => e !== name)
      : [...effEvents, name])
  }

  const save = async () => {
    setSaving(true)
    try {
      const r = await api.notifySave({
        webhook_url: url.trim() || undefined,
        format: format || undefined,
        events: effEvents,
        enabled: effEnabled,
      })
      toast.success(`已保存（写回字段：${r.write_back.changed.join(", ") || "无变化"}）`)
      r.warnings.forEach((w) => toast.warning(w))
      if (url.trim()) setUrl("")
      setFormat("")
      setEvents(null)
      setEnabled(null)
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setSaving(false)
    }
  }

  const test = async () => {
    setTesting(true)
    try {
      const r = await api.notifyTest()
      if (r.ok) toast.success(`测试通知已送达：${r.detail}`)
      else toast.error(`测试通知发送失败：${r.detail}`)
    } catch (e) {
      toast.error(`测试失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setTesting(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Webhook 通知</CardTitle>
        <CardDescription>
          调参是长时任务：会话结束或 agent 降级时推送一条消息到钉钉 / 飞书 / Slack
          等自定义机器人。webhook_url 推荐写成 ${"${ENV:变量名}"} 引用环境变量，
          避免把含 token 的机器人地址明文提交进仓库；留空保存不改动现有值。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && <div className="text-red-600 text-sm">配置加载失败：{error}</div>}
        {current && (
          <div className="text-muted-foreground space-y-1 rounded-md border p-3 text-sm">
            <div>
              当前状态：
              {current.enabled
                ? <Badge className="ml-1 bg-emerald-600/15 text-emerald-700 dark:text-emerald-400">已启用</Badge>
                : <Badge variant="secondary" className="ml-1">已停用</Badge>}
              <span className="ml-2">格式：<span className="text-foreground font-mono">{current.format}</span></span>
              <span className="ml-2">订阅事件：
                <span className="text-foreground font-mono">{current.events.join(", ") || "（无）"}</span>
              </span>
            </div>
            <div>
              当前 webhook：<span className="text-foreground font-mono">{current.webhook_url_masked || "（未设置）"}</span>
              <span className="ml-2">来源：{current.webhook_url_source}</span>
            </div>
          </div>
        )}

        <div className="space-y-1">
          <Label htmlFor="webhook">webhook_url</Label>
          <Input id="webhook" type="password" placeholder="留空=保持现状；支持 ${ENV:变量名}"
                 value={url} onChange={(e) => setUrl(e.target.value)} />
        </div>
        {url.trim() && !url.trim().startsWith("${ENV:") && (
          <p className="text-amber-600 text-xs">
            ⚠ 该地址将以明文写入 demo/configs/settings.yaml，注意不要把含 token 的配置提交到公开仓库。
          </p>
        )}

        <div className="flex flex-wrap items-end gap-4">
          <div className="space-y-1">
            <Label>机器人格式</Label>
            <Select value={format || current?.format || "generic"} onValueChange={setFormat}>
              <SelectTrigger className="w-56"><SelectValue /></SelectTrigger>
              <SelectContent>
                {NOTIFY_FORMATS.map((f) => (
                  <SelectItem key={f.value} value={f.value}>{f.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center gap-2 pb-2">
            <Switch id="notify-enabled" checked={effEnabled} onCheckedChange={setEnabled} />
            <Label htmlFor="notify-enabled">启用通知</Label>
          </div>
          {Object.entries(NOTIFY_EVENT_META).map(([name, label]) => (
            <div key={name} className="flex items-center gap-2 pb-2">
              <Switch id={`ev-${name}`} checked={effEvents.includes(name)}
                      onCheckedChange={() => toggleEvent(name)} />
              <Label htmlFor={`ev-${name}`}>{label}</Label>
            </div>
          ))}
        </div>

        <div className="flex gap-2">
          <Button variant="outline" onClick={test} disabled={testing || saving}>
            {testing ? "发送中…" : "发送测试通知"}
          </Button>
          <Button onClick={save} disabled={testing || saving}>
            {saving ? "保存中…" : "保存"}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// ------------------------------------------------------------------
// 报告
// ------------------------------------------------------------------

function ReportPanel() {
  const cohort = useCohort()
  const { data: report } = usePolling<ReportResp>(() => api.report(cohort), 15000)
  const [generating, setGenerating] = useState(false)

  const generate = async () => {
    setGenerating(true)
    try {
      await api.reportGenerate(cohort)
      toast.success("报告已重新生成")
    } catch (e) {
      toast.error(`生成失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setGenerating(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-base">
          <span>
            分析报告
            {report?.exists && (
              <span className="text-muted-foreground ml-2 text-xs font-normal">
                更新于 {report.updated}
              </span>
            )}
          </span>
          <Button variant="outline" size="sm" onClick={generate} disabled={generating}>
            {generating ? "生成中…" : "重新生成"}
          </Button>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {!report ? (
          <div className="text-muted-foreground text-sm">加载中…</div>
        ) : !report.exists ? (
          <div className="text-muted-foreground text-sm">
            还没有报告。跑一次搜索，或点「重新生成」基于现有试验数据生成。
          </div>
        ) : (
          <ScrollArea className="h-96 rounded-md border p-4">
            <pre className="font-mono text-xs leading-relaxed whitespace-pre-wrap">{report.content}</pre>
          </ScrollArea>
        )}
      </CardContent>
    </Card>
  )
}
