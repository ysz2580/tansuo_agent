import { api, type Summary } from "@/lib/api"
import { usePolling } from "@/lib/usePolling"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { CurvesChart, ProgressChart } from "@/components/CurvesChart"

function StatCard({ title, value, sub }: { title: string; value: string; sub?: string }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-muted-foreground text-sm font-normal">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold">{value}</div>
        {sub && <div className="text-muted-foreground mt-1 text-xs">{sub}</div>}
      </CardContent>
    </Card>
  )
}

function fmt(v: number | null | undefined, digits = 4): string {
  return v === null || v === undefined ? "—" : v.toFixed(digits)
}

/** 秒数 → 人类可读时长（与 CLI 进度行的 ETA≈ 格式一致）。 */
function fmtEta(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`
  return `${(seconds / 3600).toFixed(1)}h`
}

export default function DashboardPage() {
  const { data: summary, error: sumErr } = usePolling<Summary>(api.summary, 5000)
  const { data: curves } = usePolling(api.curves, 10000)

  if (sumErr) {
    return <Alert variant="destructive"><AlertDescription>加载失败：{sumErr}</AlertDescription></Alert>
  }
  if (!summary) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  const c = summary.counts
  const finished = c.completed + c.pruned + c.failed
  const maximize = summary.direction === "maximize"

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard title={`最优 ${summary.primary}`} value={fmt(summary.best?.value)}
                  sub={summary.best ? `trial#${summary.best.trial}（${maximize ? "越大越好" : "越小越好"}）` : "暂无完成试验"} />
        <StatCard title="试验统计" value={`${c.completed} 完成`}
                  sub={`${c.pruned} 剪枝 ｜ ${c.failed} 失败 ｜ ${c.running} 进行中`} />
        <StatCard title="预算" value={`${finished} / ${summary.budget_total}`}
                  sub={`剩余 ${Math.max(0, summary.budget_total - finished)} 次`
                    + (summary.eta_s !== null ? ` · ETA≈${fmtEta(summary.eta_s)}` : "")
                    + (summary.workers > 1 ? ` · ${summary.workers} 并发` : "")} />
        <StatCard title="搜索空间" value={`v${summary.space_version}`}
                  sub={summary.convergence.length > 24 ? undefined : summary.convergence} />
      </div>

      <Alert>
        <AlertDescription className="flex items-center gap-2">
          <Badge variant="secondary">收敛信号</Badge>
          {summary.convergence}
        </AlertDescription>
      </Alert>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">主指标走势（按试验序号）</CardTitle>
          </CardHeader>
          <CardContent>
            <ProgressWithTrials primary={summary.primary} maximize={maximize} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-base">学习曲线（top 试验逐 epoch）</CardTitle>
          </CardHeader>
          <CardContent>
            {curves ? (
              <CurvesChart curves={curves.curves} metric={curves.primary} />
            ) : (
              <div className="text-muted-foreground py-8 text-center text-sm">加载中…</div>
            )}
          </CardContent>
        </Card>
      </div>

      {summary.best && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">最优配置（trial#{summary.best.trial}）</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {Object.entries(summary.best.params).map(([k, v]) => (
                <Badge key={k} variant="outline" className="font-mono text-xs">
                  {k}={typeof v === "number" ? Number(v.toPrecision(4)).toString() : String(v)}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

/** 从 /api/trials 取完成试验画主指标走势（独立轮询，避免拖慢顶部卡片）。 */
function ProgressWithTrials({ primary, maximize }: { primary: string; maximize: boolean }) {
  const { data } = usePolling(api.trials, 8000)
  if (!data) return <div className="text-muted-foreground py-8 text-center text-sm">加载中…</div>
  let best = maximize ? -Infinity : Infinity
  const points = data.trials
    .filter((t) => t.state === "COMPLETE" && t.value !== null)
    .sort((a, b) => a.number - b.number)
    .map((t) => {
      best = maximize ? Math.max(best, t.value!) : Math.min(best, t.value!)
      return { number: t.number, value: t.value!, best }
    })
  return <ProgressChart points={points} primary={primary} />
}
