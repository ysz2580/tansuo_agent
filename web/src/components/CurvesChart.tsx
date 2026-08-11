import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import type { CurvePoint, TrialCurve } from "@/lib/api"

const COLORS = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2"]

/** 多条试验曲线叠加：x=epoch，每个试验一条线。 */
export function CurvesChart({
  curves,
  metric,
  height = 260,
}: {
  curves: TrialCurve[]
  metric: string
  height?: number
}) {
  const maxEpoch = Math.max(0, ...curves.flatMap((c) => c.curve.map((p) => p.epoch)))
  const rows: Record<string, number>[] = []
  for (let e = 1; e <= maxEpoch; e++) {
    const row: Record<string, number> = { epoch: e }
    for (const c of curves) {
      const p = c.curve.find((x) => x.epoch === e)
      if (p && typeof p[metric] === "number") row[`trial#${c.trial}`] = p[metric] as number
    }
    rows.push(row)
  }
  if (!curves.length || !maxEpoch) {
    return <div className="text-muted-foreground py-8 text-center text-sm">暂无曲线数据</div>
  }
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
        <XAxis dataKey="epoch" tickLine={false} fontSize={12}
               label={{ value: "epoch", position: "insideBottomRight", offset: -2, fontSize: 11 }} />
        <YAxis tickLine={false} fontSize={12} domain={["auto", "auto"]} width={56} />
        <Tooltip />
        <Legend />
        {curves.map((c, i) => (
          <Line
            key={c.trial}
            type="monotone"
            dataKey={`trial#${c.trial}`}
            stroke={COLORS[i % COLORS.length]}
            strokeWidth={2}
            dot={{ r: 2 }}
            connectNulls
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  )
}

/** 主指标随试验序号的变化 + 历史最优线。 */
export function ProgressChart({
  points,
  primary,
  height = 240,
}: {
  points: { number: number; value: number; best: number }[]
  primary: string
  height?: number
}) {
  if (!points.length) {
    return <div className="text-muted-foreground py-8 text-center text-sm">还没有完成的试验</div>
  }
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={points} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
        <XAxis dataKey="number" tickLine={false} fontSize={12}
               label={{ value: "trial#", position: "insideBottomRight", offset: -2, fontSize: 11 }} />
        <YAxis tickLine={false} fontSize={12} domain={["auto", "auto"]} width={56} />
        <Tooltip />
        <Legend />
        <Line type="monotone" dataKey="value" name={primary} stroke="#94a3b8"
              strokeWidth={1.5} dot={{ r: 3 }} />
        <Line type="monotone" dataKey="best" name={`历史最优 ${primary}`} stroke="#2563eb"
              strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}

export type { CurvePoint }
