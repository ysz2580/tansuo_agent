import { useMemo, useState } from "react"
import { api, type CompareResp, type RunInfo, type TrialCurve } from "@/lib/api"
import { usePolling } from "@/lib/usePolling"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
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

function fmt(v: number | null | undefined, digits = 4): string {
  return v === null || v === undefined ? "—" : v.toFixed(digits)
}

function fmtParam(v: unknown): string {
  if (v === null || v === undefined) return "-"
  if (typeof v === "number") return Number(v.toPrecision(4)).toString()
  return String(v)
}

/** 跨分区对比：优化目标指纹相同的分区并排比最优结果。
 *  分区选择与全局分区选择器解耦——这里天然是多分区视图。 */
export default function ComparePage() {
  const { data: runs } = usePolling(api.runs, 15000)
  const [groupHash, setGroupHash] = useState<string | null>(null)

  // 候选 = 有目标指纹、非虚拟、meta 完整的分区；按 objective_hash 分组
  const candidates = useMemo(
    () => (runs?.runs ?? []).filter((r) => r.objective_hash && !r.virtual && !r.incomplete),
    [runs])
  const groups = useMemo(() => {
    const m = new Map<string, RunInfo[]>()
    for (const r of candidates) {
      const arr = m.get(r.objective_hash!) ?? []
      arr.push(r)
      m.set(r.objective_hash!, arr)
    }
    return [...m.entries()]
  }, [candidates])

  const selectedIds = groupHash
    ? (groups.find(([h]) => h === groupHash)?.[1] ?? []).map((r) => r.id)
    : undefined
  const { data: cmp, error } = usePolling<CompareResp>(
    () => api.runsCompare(selectedIds), 10000)

  if (error) {
    return <Alert variant="destructive"><AlertDescription>加载失败：{error}</AlertDescription></Alert>
  }
  if (!runs || !cmp) return <div className="text-muted-foreground py-12 text-center">加载中…</div>
  if (!cmp.cohorts.length) {
    return <div className="text-muted-foreground py-12 text-center">还没有可对比的记录分区</div>
  }

  const dirZh = cmp.primary.direction === "maximize" ? "越大越好" : "越小越好"
  // 组内提示词版本不一致 → 监督者策略跨分区不同，差异归因需谨慎
  const promptMismatch = new Set(cmp.cohorts.map((c) => c.prompt_version)).size > 1
  // 参数名并集（按首次出现顺序）
  const paramNames: string[] = []
  for (const c of cmp.cohorts) {
    if (c.best) {
      for (const k of Object.keys(c.best.params)) {
        if (!paramNames.includes(k)) paramNames.push(k)
      }
    }
  }
  // 各分区最优试验曲线叠加：trial 字段复用为分区序号，seriesLabel 显示分区号
  const chartCurves: TrialCurve[] = cmp.cohorts.map((c, i) => ({
    trial: i, value: c.best?.value ?? 0, params: {}, curve: c.curve,
  }))
  const seriesLabel = (tc: TrialCurve) =>
    `#${cmp.cohorts[tc.trial]?.id.slice(0, 4) ?? tc.trial}`

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-muted-foreground text-sm">
          对比基准：主指标 <b>{cmp.primary.name}</b>（{dirZh}）·
          目标指纹 <code className="text-xs">{cmp.objective_hash.slice(0, 8)}</code> ·
          共 {cmp.cohorts.length} 个分区
        </span>
        {groups.length > 1 && (
          <Select value={groupHash ?? "auto"}
                  onValueChange={(v) => setGroupHash(v === "auto" ? null : v)}>
            <SelectTrigger size="sm" className="max-w-80">
              <SelectValue placeholder="目标分组" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">默认（当前目标指纹）</SelectItem>
              {groups.map(([h, arr]) => (
                <SelectItem key={h} value={h}>
                  <span className="font-mono text-xs">{h.slice(0, 8)}</span>
                  <span className="text-muted-foreground ml-1 text-xs">
                    {arr[0].primary_metric?.name ?? ""} · {arr.length} 个分区
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>

      {promptMismatch && (
        <Alert className="border-amber-500/50 bg-amber-600/10">
          <AlertDescription className="text-amber-700 dark:text-amber-400">
            组内提示词版本不一致（监督 agent 的策略在分区之间变过）——跨分区差异可能不全是
            超参带来的，请谨慎对比。
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">分区概览</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>分区</TableHead>
                  <TableHead>创建时间</TableHead>
                  <TableHead>备注</TableHead>
                  <TableHead className="text-right">试验数</TableHead>
                  <TableHead className="text-right">最优值</TableHead>
                  <TableHead>最优试验</TableHead>
                  <TableHead>代码</TableHead>
                  <TableHead>数据集</TableHead>
                  <TableHead>提示词</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {cmp.cohorts.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell className="font-mono text-xs">
                      {c.id}
                      {c.locked && <Badge variant="secondary" className="ml-1.5">db 被占用</Badge>}
                    </TableCell>
                    <TableCell className="text-xs">{c.created_at ?? "-"}</TableCell>
                    <TableCell className="text-xs">{c.note || "-"}</TableCell>
                    <TableCell className="text-right">{c.completed}</TableCell>
                    <TableCell className="text-right font-medium">{fmt(c.best?.value)}</TableCell>
                    <TableCell>{c.best ? `#${c.best.trial}` : "-"}</TableCell>
                    <TableCell className="font-mono text-xs">{c.code_hash?.slice(0, 8) ?? "-"}</TableCell>
                    <TableCell className="font-mono text-xs">{c.data_hash?.slice(0, 8) ?? "-"}</TableCell>
                    <TableCell className="font-mono text-xs">
                      v{c.prompt_version}
                      {promptMismatch && (
                        <Badge variant="outline"
                               className="ml-1.5 border-amber-500/50 text-[10px] text-amber-600 dark:text-amber-400">
                          不一致
                        </Badge>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">最优配置对比（各分区最优试验）</CardTitle>
        </CardHeader>
        <CardContent>
          {paramNames.length ? (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>参数</TableHead>
                    {cmp.cohorts.map((c) => (
                      <TableHead key={c.id} className="font-mono text-xs"
                                 title={c.id + (c.note ? ` · ${c.note}` : "")}>
                        #{c.id.slice(0, 4)}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paramNames.map((name) => (
                    <TableRow key={name}>
                      <TableCell className="font-mono text-xs">{name}</TableCell>
                      {cmp.cohorts.map((c) => (
                        <TableCell key={c.id} className="font-mono text-xs">
                          {fmtParam(c.best?.params[name])}
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <div className="text-muted-foreground py-6 text-center text-sm">
              各分区都还没有完成的试验，暂无最优配置可对比
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">最优试验学习曲线（跨分区叠加）</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <CurvesChart curves={chartCurves} metric={cmp.primary.name} seriesLabel={seriesLabel} />
          {cmp.watch.map((w) => (
            <div key={w}>
              <div className="text-muted-foreground text-xs mb-1">{w}</div>
              <CurvesChart curves={chartCurves} metric={w} height={160} seriesLabel={seriesLabel} />
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
