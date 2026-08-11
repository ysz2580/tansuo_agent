import { api, type SpaceParam } from "@/lib/api"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

function fmtNum(v: number | undefined): string {
  if (v === undefined) return "?"
  return Number(v.toPrecision(4)).toString()
}

function ParamCard({ p }: { p: SpaceParam }) {
  const frozen = p.frozen !== undefined && p.frozen !== null
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          <span className="font-mono">{p.name}</span>
          <Badge variant="secondary" className="text-xs font-normal">{p.type}</Badge>
          {p.log && <Badge variant="outline" className="text-xs font-normal">log 尺度</Badge>}
          {frozen && <Badge className="bg-blue-600/15 text-blue-700 dark:text-blue-300 text-xs font-normal">已冻结 = {String(p.frozen)}</Badge>}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1.5">
        {p.type === "choice" ? (
          <div className="flex flex-wrap gap-1">
            {(p.choices ?? []).map((c) => (
              <Badge key={String(c)} variant={frozen && p.frozen === c ? "default" : "outline"}
                     className="font-mono text-xs">
                {String(c)}
              </Badge>
            ))}
            {p.env_choices && p.env_choices.length > (p.choices ?? []).length && (
              <span className="text-muted-foreground text-xs">
                （envelope：{p.env_choices.map(String).join(", ")}）
              </span>
            )}
          </div>
        ) : (
          <div className="font-mono text-sm">
            [{fmtNum(p.low)}, {fmtNum(p.high)}]
            {p.env_low !== undefined && p.env_high !== undefined &&
             (p.env_low !== p.low || p.env_high !== p.high) && (
              <span className="text-muted-foreground ml-2 text-xs">
                envelope [{fmtNum(p.env_low)}, {fmtNum(p.env_high)}]
              </span>
            )}
          </div>
        )}
        <p className="text-muted-foreground text-xs leading-relaxed">{p.description}</p>
      </CardContent>
    </Card>
  )
}

export default function SpacePage() {
  const { data, error } = usePolling(api.space, 8000)

  if (error) return <div className="text-red-600 py-8 text-center">加载失败：{error}</div>
  if (!data) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  return (
    <div className="space-y-5">
      <div className="text-muted-foreground text-sm">
        当前空间 <span className="font-mono">v{data.version}</span> ｜
        自由参数 {data.free_params} 个，冻结 {data.params.length - data.free_params} 个
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {data.params.map((p) => <ParamCard key={p.name} p={p} />)}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">补丁历史（agent 的空间编辑）</CardTitle>
        </CardHeader>
        <CardContent>
          {data.patches.length === 0 ? (
            <div className="text-muted-foreground text-sm">
              还没有空间编辑记录——搜索仍在初始空间内进行。
            </div>
          ) : (
            <ol className="space-y-3">
              {data.patches.map((patch, i) => (
                <li key={i} className="border-l-2 pl-3">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <Badge variant="secondary" className="font-mono">v{patch.version}</Badge>
                    <span className="text-muted-foreground text-xs">{patch.ts}</span>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {patch.ops.map((op, j) => (
                      <Badge key={j} variant="outline" className="font-mono text-xs">
                        {op.op}({op.param}{op.low !== undefined
                          ? ` → [${fmtNum(op.low)}, ${fmtNum(op.high)}]`
                          : op.value !== undefined ? ` → ${String(op.value)}` : ""})
                      </Badge>
                    ))}
                  </div>
                  <p className="text-muted-foreground mt-1 text-xs">理由：{patch.rationale}</p>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
