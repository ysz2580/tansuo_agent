import { useState } from "react"
import { toast } from "sonner"
import {
  api,
  type AgentConfig,
  type ProbeResult,
  type ReportResp,
  type RunLogResp,
} from "@/lib/api"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Switch } from "@/components/ui/switch"

export default function SettingsPage() {
  return (
    <div className="space-y-4">
      <RunPanel />
      <ApiConfigPanel />
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
  const [fresh, setFresh] = useState(false)
  const [busy, setBusy] = useState(false)

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
        fresh,
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
          启动一次搜索（子进程方式，等价于 python cli.py run）；「清空重来」会删除历史 db/journal/快照。
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
            <Switch id="fresh" checked={fresh} onCheckedChange={setFresh} />
            <Label htmlFor="fresh">清空重来</Label>
          </div>
          <div className="ml-auto flex gap-2">
            <Button onClick={start} disabled={running || busy}>开始搜索</Button>
            <Button variant="destructive" onClick={stop} disabled={!running || busy}>停止</Button>
          </div>
        </div>

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
// API 配置切换
// ------------------------------------------------------------------

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
// 报告
// ------------------------------------------------------------------

function ReportPanel() {
  const { data: report } = usePolling<ReportResp>(api.report, 15000)
  const [generating, setGenerating] = useState(false)

  const generate = async () => {
    setGenerating(true)
    try {
      await api.reportGenerate()
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
