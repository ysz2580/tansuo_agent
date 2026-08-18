import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { api } from "@/lib/api"
import { useCohort } from "@/lib/cohort"
import { groupByRounds } from "@/lib/agentEvents"
import { usePolling } from "@/lib/usePolling"
import { AgentTimeline } from "@/components/AgentTimeline"

/** 「完整监督历史」子页面：当前分区全量轮次时间线，最新在前。
 *  Agent 主屏只展示最新一轮；历史回溯进这里。
 *  Dialog 打开才挂载 → 轮询随开合自动启停。 */
export function HistoryDialog({ open, onOpenChange }: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] w-[min(96vw,64rem)] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle>监督历史（全部轮次）</DialogTitle>
          <DialogDescription>当前分区的每一轮唤醒：看到什么 → 判断什么 → 做了什么；最新在前</DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {open && <HistoryBody />}
        </div>
      </DialogContent>
    </Dialog>
  )
}

function HistoryBody() {
  const cohort = useCohort()
  const { data, error } = usePolling(() => api.agentEvents(cohort), 8000)

  if (error) return <div className="text-red-600 py-8 text-center">加载失败：{error}</div>
  if (!data) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  const segments = groupByRounds(data.events)
  const reversed = [...segments].reverse()

  return (
    <div className="space-y-3">
      <div className="text-muted-foreground flex flex-wrap items-center gap-3 text-sm">
        <span>共 {data.tokens.rounds} 轮唤醒 · {data.events.length} 条 agent 事件</span>
        {data.tokens.total_tokens > 0 && (
          <span className="text-blue-700 dark:text-blue-400">
            累计 tokens：{data.tokens.total_tokens.toLocaleString()}
            （in {data.tokens.input_tokens.toLocaleString()} /
            out {data.tokens.output_tokens.toLocaleString()}）
          </span>
        )}
      </div>
      <AgentTimeline
        segments={reversed}
        emptyHint={(
          <div className="text-muted-foreground py-12 text-center text-sm">
            尚无监督记录：启动搜索后这里按轮次呈现 agent 的判断与动作。
          </div>
        )}
      />
    </div>
  )
}
