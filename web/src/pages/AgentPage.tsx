import { useState } from "react"
import { BotIcon, HistoryIcon, Loader2Icon, Settings2Icon } from "lucide-react"
import { api, type SetupStatus } from "@/lib/api"
import { useCohort } from "@/lib/cohort"
import { groupByRounds } from "@/lib/agentEvents"
import { usePolling } from "@/lib/usePolling"
import { AgentTimeline } from "@/components/AgentTimeline"
import { HistoryDialog } from "@/components/HistoryDialog"
import { SetupDialog } from "@/components/SetupDialog"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"

/** Agent 主页（默认首页）：主屏只回答一个问题——「agent 现在/刚才在干什么」。
 *  最新一轮监督卡片直接呈现；完整历史与 setup（含长日志）收进子页面 Dialog，
 *  避免多件事混在一屏。 */
export default function AgentPage() {
  const cohort = useCohort()
  const [setupOpen, setSetupOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const { data, error } = usePolling(() => api.agentEvents(cohort), 8000)
  const { data: setupStatus } = usePolling<SetupStatus>(api.setupStatus, 3000)

  if (error) return <div className="text-red-600 py-8 text-center">加载失败：{error}</div>
  if (!data) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  const segments = groupByRounds(data.events)
  const latest = segments.length > 0 ? segments[segments.length - 1] : null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="outline" size="sm" onClick={() => setSetupOpen(true)}
                title="阅读训练脚本、起草 settings 与搜索空间（含实时日志）">
          <Settings2Icon className="size-3.5" /> 配置 agent（setup）
        </Button>
        {segments.length > 0 && (
          <Button variant="outline" size="sm" onClick={() => setHistoryOpen(true)}
                  title="当前分区全部唤醒轮次，最新在前">
            <HistoryIcon className="size-3.5" /> 完整监督历史（{data.tokens.rounds} 轮）
          </Button>
        )}
        {data.tokens.total_tokens > 0 && (
          <span className="text-muted-foreground ml-auto text-sm"
                title="本分区所有调参唤醒累计的大模型 token 用量">
            累计 tokens：{data.tokens.total_tokens.toLocaleString()}
          </span>
        )}
      </div>

      {setupStatus?.running && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-3 pt-6 text-sm">
            <Badge className="bg-emerald-600/15 text-emerald-700 dark:text-emerald-400">
              <Loader2Icon className="size-3 animate-spin" /> 配置 agent 进行中
            </Badge>
            <span className="text-muted-foreground">正在阅读训练脚本并起草配置…</span>
            <Button variant="outline" size="sm" className="ml-auto"
                    onClick={() => setSetupOpen(true)}>
              查看进度与日志
            </Button>
          </CardContent>
        </Card>
      )}

      {latest ? (
        <div className="space-y-2">
          <h2 className="text-muted-foreground text-sm font-medium">最新监督轮次</h2>
          <AgentTimeline segments={[latest]} />
        </div>
      ) : (
        <div className="space-y-3 rounded-md border border-dashed py-16 text-center">
          <BotIcon className="text-muted-foreground mx-auto size-8" />
          <p className="font-medium">尚无监督记录</p>
          <p className="text-muted-foreground mx-auto max-w-md text-sm">
            启动搜索后，agent 每若干次试验唤醒一轮，这里直接呈现它最新一轮的
            「看到什么 → 判断什么 → 做了什么」。
          </p>
          {!setupStatus?.running && (
            <Button variant="outline" size="sm" onClick={() => setSetupOpen(true)}>
              <Settings2Icon className="size-3.5" /> 先去配置 agent（setup）
            </Button>
          )}
        </div>
      )}

      <SetupDialog open={setupOpen} onOpenChange={setSetupOpen} />
      <HistoryDialog open={historyOpen} onOpenChange={setHistoryOpen} />
    </div>
  )
}
