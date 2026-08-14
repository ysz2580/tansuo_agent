import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"

const COLORS = ["#2563eb", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#dc2626"]

/** 参数重要度（optuna PED-ANOVA，归一化之和≈1）：横向条形图，按重要度降序。 */
export function ImportanceChart({
  importances,
  height = 200,
}: {
  importances: Record<string, number>
  height?: number
}) {
  const data = Object.entries(importances)
    .sort((a, b) => b[1] - a[1])
    .map(([name, value]) => ({ name, value }))
  if (!data.length) {
    return <div className="text-muted-foreground py-8 text-center text-sm">试验过少，暂无重要度</div>
  }
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} layout="vertical" margin={{ top: 8, right: 24, bottom: 0, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" className="stroke-muted" horizontal={false} />
        <XAxis type="number" domain={[0, 1]} tickLine={false} fontSize={12} />
        <YAxis type="category" dataKey="name" tickLine={false} fontSize={12} width={120} />
        <Tooltip formatter={(v) => (typeof v === "number" ? v.toFixed(3) : String(v))} />
        <Bar dataKey="value" name="重要度" barSize={16}>
          {data.map((d, i) => (
            <Cell key={d.name} fill={COLORS[i % COLORS.length]} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}
