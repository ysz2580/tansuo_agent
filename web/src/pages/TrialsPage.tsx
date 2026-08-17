import { useEffect, useState } from "react"
import { toast } from "sonner"
import { api, type CurveResp, type SpaceParam, type SpaceResp, type Trial, type TrialLogResp } from "@/lib/api"
import { useCohort } from "@/lib/cohort"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { CurvesChart } from "@/components/CurvesChart"

const STATE_META: Record<Trial["state"], { label: string; cls: string }> = {
  COMPLETE: { label: "完成", cls: "bg-emerald-600/15 text-emerald-700 dark:text-emerald-400" },
  PRUNED: { label: "剪枝", cls: "bg-amber-600/15 text-amber-700 dark:text-amber-400" },
  FAIL: { label: "失败", cls: "bg-red-600/15 text-red-700 dark:text-red-400" },
  RUNNING: { label: "进行中", cls: "bg-blue-600/15 text-blue-700 dark:text-blue-400" },
  WAITING: { label: "等待", cls: "bg-gray-600/15 text-gray-600 dark:text-gray-400" },
}

function fmt(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : v.toFixed(4)
}

export default function TrialsPage() {
  const cohort = useCohort()
  const { data, error } = usePolling(() => api.trials(cohort), 5000)
  const [selected, setSelected] = useState<Trial | null>(null)

  if (error) return <div className="text-red-600 py-8 text-center">加载失败：{error}</div>
  if (!data) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  const trials = [...data.trials].sort((a, b) => b.number - a.number)

  return (
    <div className="space-y-3">
      <CustomTrialCard cohort={cohort} />
      <div className="text-muted-foreground text-sm">
        共 {trials.length} 个试验 ｜ 主指标 <span className="font-mono">{data.primary}</span>
        （{data.direction === "maximize" ? "越大越好" : "越小越好"}）｜ 点击行查看完整参数与学习曲线
      </div>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-16">#</TableHead>
              <TableHead className="w-20">状态</TableHead>
              <TableHead className="w-28">{data.primary}</TableHead>
              <TableHead className="w-24">耗时</TableHead>
              <TableHead>参数摘要</TableHead>
              <TableHead className="w-40">备注</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {trials.map((t) => {
              const meta = STATE_META[t.state] ?? STATE_META.WAITING
              const brief = Object.entries(t.params)
                .slice(0, 4)
                .map(([k, v]) => `${k}=${typeof v === "number" ? Number(v.toPrecision(3)) : v}`)
                .join("  ")
              return (
                <TableRow key={t.number} className="cursor-pointer"
                          onClick={() => setSelected(t)}>
                  <TableCell className="font-mono">{t.number}</TableCell>
                  <TableCell>
                    <Badge variant="outline" className={meta.cls}>{meta.label}</Badge>
                  </TableCell>
                  <TableCell className="font-mono">{fmt(t.value)}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {t.duration_s !== null ? `${t.duration_s}s` : "—"}
                  </TableCell>
                  <TableCell className="text-muted-foreground max-w-96 truncate font-mono text-xs">
                    {brief}
                  </TableCell>
                  <TableCell className="text-muted-foreground max-w-48 truncate text-xs">
                    {t.fail_reason ?? (t.attrs.note as string | undefined) ??
                     (t.attrs.source === "custom" ? "agent 假设试验" : "")}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      <TrialDetailDialog trial={selected} cohort={cohort} onClose={() => setSelected(null)} />
    </div>
  )
}

function TrialDetailDialog({ trial, cohort, onClose }:
                           { trial: Trial | null; cohort: string | null; onClose: () => void }) {
  const [curve, setCurve] = useState<CurveResp | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [logText, setLogText] = useState<string | null>(null)

  // 打开新试验时按需拉曲线与全量日志；关闭时清空
  useEffect(() => {
    if (!trial) {
      setCurve(null)
      setErr(null)
      setLoading(false)
      setLogText(null)
      return
    }
    let active = true
    setLoading(true)
    setCurve(null)
    setErr(null)
    setLogText(null)
    api.trialCurve(trial.number, cohort)
      .then((c) => active && setCurve(c))
      .catch((e: Error) => active && setErr(e.message))
      .finally(() => active && setLoading(false))
    if (trial.has_log) {
      api.trialLog(trial.number, cohort)
        .then((r: TrialLogResp) => active && setLogText(r.text))
        .catch(() => active && setLogText(null))
    }
    return () => {
      active = false
    }
  }, [trial, cohort])

  return (
    <Dialog open={trial !== null}
            onOpenChange={(open) => {
              if (!open) {
                onClose()
                setCurve(null)
                setErr(null)
                setLoading(false)
                setLogText(null)
              }
            }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>trial#{trial?.number} 详情</DialogTitle>
        </DialogHeader>
        {trial && (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(trial.params).map(([k, v]) => (
                <Badge key={k} variant="outline" className="font-mono text-xs">
                  {k}={typeof v === "number" ? Number(v.toPrecision(4)).toString() : String(v)}
                </Badge>
              ))}
            </div>
            {trial.fail_reason && (
              <div className="text-red-600 text-sm">失败原因：{trial.fail_reason}</div>
            )}
            <div>
              <div className="mb-1 text-sm font-medium">逐 epoch 学习曲线</div>
              {loading && <div className="text-muted-foreground text-sm">加载中…</div>}
              {err && <div className="text-muted-foreground text-sm">{err}</div>}
              {curve && (
                <div className="space-y-3">
                  <CurvesChart curves={[curve.curve]} metric={curve.primary} height={200} />
                  {curve.watch.map((w) => (
                    <div key={w}>
                      <div className="text-muted-foreground mb-1 text-xs">{w}</div>
                      <CurvesChart curves={[curve.curve]} metric={w} height={140} />
                    </div>
                  ))}
                </div>
              )}
            </div>
            {trial.has_log && (
              <div>
                <div className="mb-1 text-sm font-medium">完整输出日志（stdout / stderr）</div>
                {logText === null ? (
                  <div className="text-muted-foreground text-sm">日志加载中或不可用…</div>
                ) : (
                  <ScrollArea className="h-56 rounded-md border bg-neutral-950 p-3">
                    <pre className="font-mono text-xs leading-relaxed whitespace-pre-wrap text-neutral-200">
                      {logText}
                    </pre>
                  </ScrollArea>
                )}
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------
// 人工插队试验：用户直接投一组参数，空闲时即时执行，运行中排队等批边界消费
// ------------------------------------------------------------------

function defaultFor(p: SpaceParam): string | number {
  if (p.type === "choice") {
    return p.choices && p.choices.length > 0 ? String(p.choices[0]) : ""
  }
  const low = p.low ?? 0
  const high = p.high ?? 1
  if (p.log && low > 0 && high > 0) {
    return Number(Math.sqrt(low * high).toPrecision(6))
  }
  const mid = (low + high) / 2
  return p.type === "int" ? Math.round(mid) : Number(mid.toPrecision(6))
}

function CustomTrialCard({ cohort }: { cohort: string | null }) {
  const [space, setSpace] = useState<SpaceResp | null>(null)
  const [spaceErr, setSpaceErr] = useState<string | null>(null)
  const [values, setValues] = useState<Record<string, string>>({})
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let active = true
    setSpace(null)
    setSpaceErr(null)
    api.space(cohort)
      .then((s) => {
        if (!active) return
        setSpace(s)
        const init: Record<string, string> = {}
        for (const p of s.params) {
          if (p.frozen !== undefined && p.frozen !== null) continue
          init[p.name] = String(defaultFor(p))
        }
        setValues(init)
      })
      .catch((e: Error) => active && setSpaceErr(e.message))
    return () => {
      active = false
    }
  }, [cohort])

  const submit = async () => {
    if (!space) return
    const params: Record<string, unknown> = {}
    for (const p of space.params) {
      if (p.frozen !== undefined && p.frozen !== null) continue
      const raw = values[p.name]
      if (p.type === "choice") {
        // 还原原始类型（choice 可能是数值；Select 只能传字符串）
        const match = (p.choices ?? []).find((c) => String(c) === raw)
        params[p.name] = match !== undefined ? match : raw
      } else {
        const n = Number(raw)
        if (!Number.isFinite(n)) {
          toast.error(`参数 ${p.name} 不是有效数字：${raw}`)
          return
        }
        params[p.name] = p.type === "int" ? Math.round(n) : n
      }
    }
    setBusy(true)
    try {
      const r = await api.customTrial({ params, note: note.trim() || undefined })
      if (r.mode === "executing") toast.success(`人工试验即时执行中（分区 ${r.cohort}），进度看运行日志`)
      else toast.success(`已排队到分区 ${r.cohort}：${r.detail}`)
    } catch (e) {
      toast.error(`提交失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  if (spaceErr) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">人工插队试验</CardTitle>
          <CardDescription>搜索空间加载失败：{spaceErr}</CardDescription>
        </CardHeader>
      </Card>
    )
  }

  const frozenParams = space?.params.filter((p) => p.frozen !== undefined && p.frozen !== null) ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">人工插队试验</CardTitle>
        <CardDescription>
          不等 agent，直接投一组你想试的参数：搜索空闲时立即执行，运行中则排队、下一批开头自动执行
          （journal source=human 可审计）。默认值为区间中点/首选项，按需修改后提交。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {!space && !spaceErr && <div className="text-muted-foreground text-sm">加载中…</div>}
        {space && (
          <>
            <div className="flex flex-wrap items-end gap-4">
              {space.params.map((p) => {
                if (p.frozen !== undefined && p.frozen !== null) return null
                if (p.type === "choice") {
                  return (
                    <div key={p.name} className="space-y-1">
                      <Label>{p.name}</Label>
                      <Select value={values[p.name] ?? ""}
                              onValueChange={(v) => setValues((cur) => ({ ...cur, [p.name]: v }))}>
                        <SelectTrigger className="w-36"><SelectValue /></SelectTrigger>
                        <SelectContent>
                          {(p.choices ?? []).map((c) => (
                            <SelectItem key={String(c)} value={String(c)}>{String(c)}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  )
                }
                return (
                  <div key={p.name} className="space-y-1">
                    <Label>{p.name}</Label>
                    <Input className="w-32" value={values[p.name] ?? ""}
                           onChange={(e) => setValues((cur) => ({ ...cur, [p.name]: e.target.value }))} />
                    <div className="text-muted-foreground text-xs">
                      [{p.low}, {p.high}]{p.log ? "（对数）" : ""}
                    </div>
                  </div>
                )
              })}
              <div className="space-y-1">
                <Label>备注</Label>
                <Input className="w-40" placeholder="可选" value={note}
                       onChange={(e) => setNote(e.target.value)} />
              </div>
              <Button onClick={submit} disabled={busy || space.free_params === 0}>
                {busy ? "提交中…" : "提交人工试验"}
              </Button>
            </div>
            {frozenParams.length > 0 && (
              <div className="text-muted-foreground flex flex-wrap items-center gap-1.5 text-xs">
                已冻结（自动带入，不可改）：
                {frozenParams.map((p) => (
                  <Badge key={p.name} variant="secondary" className="font-mono text-xs">
                    {p.name}={String(p.frozen)}
                  </Badge>
                ))}
              </div>
            )}
            {space.free_params === 0 && (
              <div className="text-muted-foreground text-xs">当前空间没有可自由调节的参数，无法提交人工试验。</div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
