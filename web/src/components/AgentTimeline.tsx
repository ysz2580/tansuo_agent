import type { ReactNode } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { AgentEvent } from "@/lib/api"
import { eventBody, KIND_META, type AgentSegment } from "@/lib/agentEvents"

/** Agent 监督时间线：按唤醒轮次把事件流讲成叙事——
 *  每轮一张卡：看到什么（上下文+护栏信号）→ 判断什么（结论 note）→ 做了什么（工具动作）。
 *  tune/setup 共用本组件（setup 整条会话一段）。 */
export function AgentTimeline({ segments, emptyHint }: {
  segments: AgentSegment[]
  emptyHint?: ReactNode
}) {
  if (segments.length === 0) {
    return emptyHint ? <>{emptyHint}</> : null
  }
  return (
    <div className="space-y-4">
      {segments.map((seg, i) => <SegmentCard key={i} seg={seg} />)}
    </div>
  )
}

function SegmentCard({ seg }: { seg: AgentSegment }) {
  const isSetup = seg.mode === "setup"
  const title = isSetup ? "配置会话" : `第 ${seg.round} 轮`
  const tsRange = seg.startTs
    ? seg.endTs && seg.endTs !== seg.startTs
      ? `${seg.startTs} ~ ${seg.endTs}`
      : seg.startTs
    : seg.endTs

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          {title}
          {tsRange && (
            <span className="text-muted-foreground font-mono text-xs font-normal">{tsRange}</span>
          )}
          {seg.tokens && (
            <Badge variant="outline"
                   className="ml-auto bg-blue-600/10 text-xs font-normal text-blue-700 dark:text-blue-400"
                   title="本轮唤醒的大模型 token 用量">
              in {seg.tokens.in.toLocaleString()} / out {seg.tokens.out.toLocaleString()}
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <Section label="看到什么">
          <SeenBlock seg={seg} />
        </Section>
        {seg.judgment && (
          <Section label="判断什么">
            <pre className="text-foreground max-h-56 overflow-y-auto rounded-md bg-muted/50 p-3 font-mono text-[13px] leading-6 whitespace-pre-wrap">
              {seg.judgment}
            </pre>
          </Section>
        )}
        {seg.toolCalls.length > 0 && (
          <Section label={`做了什么（${seg.toolCalls.length} 项动作）`}>
            <ul className="space-y-2">
              {seg.toolCalls.map((e, i) => <ActionRow key={i} e={e} />)}
            </ul>
          </Section>
        )}
      </CardContent>
    </Card>
  )
}

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="text-muted-foreground text-xs font-medium tracking-wide">{label}</div>
      {children}
    </div>
  )
}

/** 看到什么：结构化上下文 + 护栏信号徽章。 */
function SeenBlock({ seg }: { seg: AgentSegment }) {
  const ctx = seg.context
  const parts: string[] = []
  if (seg.mode === "setup") {
    if (ctx.trainScript) parts.push(`训练脚本：${ctx.trainScript}`)
    parts.push("任务：阅读训练脚本 → 推断指标/预算 → 起草 settings 与搜索空间")
  } else {
    if (ctx.budgetLeft !== undefined) parts.push(`剩余预算 ${ctx.budgetLeft}`)
    if (ctx.spaceVersion !== undefined) parts.push(`空间 v${ctx.spaceVersion}`)
  }
  return (
    <div className="space-y-1.5 text-sm">
      {parts.length > 0 && <div className="text-muted-foreground">{parts.join(" ｜ ")}</div>}
      {ctx.signals && ctx.signals.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {ctx.signals.map((s, i) => (
            <Badge key={i} variant="outline"
                   className="border-amber-500/40 bg-amber-600/15 text-xs font-normal text-amber-700 dark:text-amber-400">
              ⚠ {s}
            </Badge>
          ))}
        </div>
      )}
      {parts.length === 0 && !ctx.signals?.length && (
        <div className="text-muted-foreground">（本轮无额外上下文）</div>
      )}
    </div>
  )
}

/** 做了什么：单条动作。edit_search_space 高亮展开 ops+rationale（空间改动并入时间线）。 */
function ActionRow({ e }: { e: AgentEvent }) {
  const meta = KIND_META[e.kind] ?? { label: e.kind, cls: "" }
  const isEdit = e.kind === "agent_tool_call" && e.tool === "edit_search_space"
  const input = (e.input ?? null) as Record<string, unknown> | null

  if (isEdit && input) {
    const ops = Array.isArray(input.ops) ? input.ops as Record<string, unknown>[] : []
    const rationale = typeof input.rationale === "string" ? input.rationale : ""
    return (
      <li className="rounded-md border border-violet-500/40 bg-violet-600/5 p-2.5">
        <div className="flex items-start gap-2 text-sm">
          <Badge variant="outline" className={`${meta.cls} shrink-0`}>空间编辑</Badge>
          {e.allowed === false && (
            <Badge variant="outline" className="shrink-0 border-red-500/40 bg-red-600/15 text-red-700 dark:text-red-400">
              被权限策略拦截
            </Badge>
          )}
          <span className="text-muted-foreground shrink-0 font-mono text-xs leading-5">{e.ts}</span>
        </div>
        {ops.length > 0 && (
          <ul className="text-foreground mt-1.5 space-y-0.5 pl-1 font-mono text-xs">
            {ops.map((op, i) => (
              <li key={i}>
                <span className="text-violet-700 dark:text-violet-400">{String(op.op ?? "?")}</span>
                {" · "}{String(op.param ?? "?")}
                {op.low !== undefined || op.high !== undefined
                  ? ` → [${op.low ?? "?"}, ${op.high ?? "?"}]`
                  : ""}
                {op.value !== undefined ? ` → ${JSON.stringify(op.value)}` : ""}
              </li>
            ))}
          </ul>
        )}
        {rationale && (
          <p className="text-muted-foreground mt-1.5 text-[13px] leading-5">理由：{rationale}</p>
        )}
      </li>
    )
  }

  return (
    <li className="flex items-start gap-2 border-b pb-2 text-sm last:border-0">
      <Badge variant="outline" className={`${meta.cls} shrink-0`}>{meta.label}</Badge>
      <span className="text-muted-foreground shrink-0 font-mono text-xs leading-5">{e.ts}</span>
      <span className="break-all leading-5">{eventBody(e)}</span>
    </li>
  )
}
