import { useEffect, useState } from "react"
import { api, type CurveResp, type Trial } from "@/lib/api"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
  const { data, error } = usePolling(api.trials, 5000)
  const [selected, setSelected] = useState<Trial | null>(null)

  if (error) return <div className="text-red-600 py-8 text-center">加载失败：{error}</div>
  if (!data) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  const trials = [...data.trials].sort((a, b) => b.number - a.number)

  return (
    <div className="space-y-3">
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

      <TrialDetailDialog trial={selected} onClose={() => setSelected(null)} />
    </div>
  )
}

function TrialDetailDialog({ trial, onClose }: { trial: Trial | null; onClose: () => void }) {
  const [curve, setCurve] = useState<CurveResp | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  // 打开新试验时按需拉曲线；关闭时清空
  useEffect(() => {
    if (!trial) {
      setCurve(null)
      setErr(null)
      setLoading(false)
      return
    }
    let active = true
    setLoading(true)
    setCurve(null)
    setErr(null)
    api.trialCurve(trial.number)
      .then((c) => active && setCurve(c))
      .catch((e: Error) => active && setErr(e.message))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [trial])

  return (
    <Dialog open={trial !== null}
            onOpenChange={(open) => {
              if (!open) {
                onClose()
                setCurve(null)
                setErr(null)
                setLoading(false)
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
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
