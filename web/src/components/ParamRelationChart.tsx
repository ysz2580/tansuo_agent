import { useState } from "react"
import {
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import type { Trial } from "@/lib/api"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

type Pt = { number: number; x: number | string; y: number; isBest: boolean }

/** 参数-取值关系探索图：任选一个参数，散点展示其取值与主指标的关系。
 *  数值参数走数值轴，choice/字符串参数走类别轴；最优试验红点高亮。 */
export function ParamRelationChart({
  trials,
  primary,
  direction,
  height = 260,
}: {
  trials: Trial[]
  primary: string
  direction: string
  height?: number
}) {
  const done = trials.filter((t) => t.state === "COMPLETE" && t.value !== null)
  // 参数清单 = 完成试验 params 键的并集（条件参数可能只在部分试验出现）
  const paramNames = [...new Set(done.flatMap((t) => Object.keys(t.params)))].sort()
  const [selected, setSelected] = useState("")
  const param = paramNames.includes(selected) ? selected : paramNames[0]

  let bestNumber = -1
  if (done.length) {
    const bestTrial = done.reduce((a, b) =>
      direction === "maximize"
        ? (a.value! >= b.value! ? a : b)
        : (a.value! <= b.value! ? a : b),
    )
    bestNumber = bestTrial.number
  }

  const pts: Pt[] = param
    ? done
        .filter((t) => param in t.params)
        .map((t) => ({ number: t.number, x: t.params[param], y: t.value!, isBest: t.number === bestNumber }))
    : []
  const categorical = pts.some((p) => typeof p.x === "string")
  pts.sort((a, b) =>
    categorical ? String(a.x).localeCompare(String(b.x)) : Number(a.x) - Number(b.x),
  )

  if (!paramNames.length) {
    return <div className="text-muted-foreground py-8 text-center text-sm">还没有完成的试验，暂无参数样本</div>
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3">
        <span className="text-muted-foreground text-sm shrink-0">选参数</span>
        <Select value={param} onValueChange={setSelected}>
          <SelectTrigger className="w-44">
            <SelectValue placeholder="选择参数" />
          </SelectTrigger>
          <SelectContent>
            {paramNames.map((n) => (
              <SelectItem key={n} value={n}>{n}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-muted-foreground text-xs">{pts.length} 次试验样本</span>
      </div>
      {pts.length === 0 ? (
        <div className="text-muted-foreground py-8 text-center text-sm">该参数在完成试验中没有样本（可能是条件参数）</div>
      ) : (
        <>
          <ResponsiveContainer width="100%" height={height}>
            <ScatterChart margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              {categorical ? (
                <XAxis dataKey="x" type="category" allowDuplicatedCategory={false}
                       tickLine={false} fontSize={12}
                       label={{ value: param, position: "insideBottomRight", offset: -2, fontSize: 11 }} />
              ) : (
                <XAxis dataKey="x" type="number" domain={["auto", "auto"]}
                       tickLine={false} fontSize={12}
                       label={{ value: param, position: "insideBottomRight", offset: -2, fontSize: 11 }} />
              )}
              <YAxis dataKey="y" type="number" tickLine={false} fontSize={12}
                     domain={["auto", "auto"]} width={56} />
              <Tooltip
                content={(props) => {
                  const { active, payload } = props
                  if (!active || !payload || !payload.length) return null
                  const d = payload[0].payload as Pt
                  return (
                    <div className="bg-background rounded border px-2 py-1 text-xs shadow">
                      <div>trial#{d.number}{d.isBest ? "（最优）" : ""}</div>
                      <div>{param} = {String(d.x)}</div>
                      <div>{primary} = {d.y}</div>
                    </div>
                  )
                }}
              />
              <Scatter data={pts} name={primary}>
                {pts.map((p) => (
                  <Cell key={p.number} fill={p.isBest ? "#dc2626" : "#2563eb"} />
                ))}
              </Scatter>
            </ScatterChart>
          </ResponsiveContainer>
          <div className="text-muted-foreground text-xs">
            红点 = 当前最优试验（trial#{bestNumber}）；方向：{direction === "maximize" ? "越大越好" : "越小越好"}
          </div>
        </>
      )}
    </div>
  )
}
