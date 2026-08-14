import { useState } from "react"
import { toast } from "sonner"
import { Loader2Icon, PlayIcon, SquareIcon } from "lucide-react"
import { api, type SetupLogResp, type SetupStatus } from "@/lib/api"
import { eventBody, KIND_META } from "@/lib/agentEvents"
import { useProject } from "@/lib/project"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

/** 配置 agent（setup）面板：新建项目后手动触发——LLM 阅读训练脚本，
 *  起草 settings.yaml + search_space.yaml（含探针验证），进度实时流到
 *  下方日志与事件列表（setup_journal.jsonl，kind 与调参会话同构）。
 *
 *  与搜索硬互斥：任一在跑时另一边返回 409（toast 显示后端原因）。 */
export default function SetupPanel() {
  const { project } = useProject()
  const [acting, setActing] = useState(false)
  const { data: status } = usePolling<SetupStatus>(api.setupStatus, 3000)
  const running = status?.running ?? false
  // 运行中高频拉日志/事件；空闲低频兜底（展示上一轮残留状态与历史会话事件）
  const { data: log } = usePolling<SetupLogResp>(api.setupLog, running ? 2000 : 15000)
  const { data: ev } = usePolling(api.setupEvents, running ? 4000 : 15000)

  const start = async () => {
    if (!project) return
    setActing(true)
    try {
      await api.setupStart(project.id)
      toast.success("配置 agent 已启动：正在阅读训练脚本并起草配置")
    } catch (e) {
      toast.error(`启动失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setActing(false)
    }
  }

  const stop = async () => {
    setActing(true)
    try {
      await api.setupStop()
      toast.success("已请求停止配置会话")
    } catch (e) {
      toast.error(`停止失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setActing(false)
    }
  }

  const statusBadge = !status ? null
    : running ? (
      <Badge className="bg-emerald-600/15 text-emerald-700 dark:text-emerald-400">
        <Loader2Icon className="size-3 animate-spin" /> 配置中…
      </Badge>
    ) : status.exit_code === null ? (
      <Badge variant="outline">尚未运行</Badge>
    ) : status.exit_code === 0 ? (
      <Badge className="bg-emerald-600/15 text-emerald-700 dark:text-emerald-400">上次配置完成</Badge>
    ) : (
      <Badge className="bg-red-600/15 text-red-700 dark:text-red-400">
        上次退出码 {status.exit_code}（详见日志）
      </Badge>
    )

  const noScript = !!project && !project.train_script

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-3 text-base">
            配置 agent
            {statusBadge}
            <div className="ml-auto flex items-center gap-2">
              {running ? (
                <Button variant="outline" size="sm" onClick={stop} disabled={acting}>
                  <SquareIcon className="size-3.5" /> 停止
                </Button>
              ) : (
                <Button size="sm" onClick={start}
                        disabled={acting || !project || noScript}
                        title={noScript ? "该项目未登记训练脚本：新建项目时请选择主训练脚本" : undefined}>
                  <PlayIcon className="size-3.5" /> 开始配置
                </Button>
              )}
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="text-muted-foreground space-y-1 text-sm">
          {project ? (
            <>
              <p>
                对当前项目 <span className="text-foreground font-medium">{project.name}</span>
                （<span className="font-mono text-xs">{project.dir}</span>）运行配置 agent：
                阅读训练脚本 → 推断指标/预算/驱动方式 → 起草 settings.yaml 与
                search_space.yaml，并做端点探针验证。
              </p>
              <p>
                训练脚本：
                {project.train_script
                  ? <span className="font-mono text-xs">{project.train_script}</span>
                  : "未登记（新建项目时选择，或改用 CLI `python cli.py init` 离线模板）"}
              </p>
              <p className="text-xs">
                注意：与搜索运行互斥；配置会覆写项目的
                .tansuo/settings.yaml 与 search_space.yaml。
              </p>
            </>
          ) : (
            <p>项目信息加载中…</p>
          )}
        </CardContent>
      </Card>

      {(log?.text || running) && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">实时日志</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="bg-muted/50 max-h-64 overflow-y-auto rounded-md p-3 font-mono text-xs leading-5 whitespace-pre-wrap">
              {log?.text || "（等待输出…）"}
            </pre>
          </CardContent>
        </Card>
      )}

      {ev && ev.events.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              会话事件（{ev.events.length} 条）
              {ev.tokens.total_tokens > 0 && (
                <span className="text-muted-foreground ml-3 text-xs font-normal">
                  累计 tokens：{ev.tokens.total_tokens.toLocaleString()}
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ol className="space-y-2">
              {ev.events.map((e, i) => {
                const meta = KIND_META[e.kind] ?? { label: e.kind, cls: "" }
                return (
                  <li key={i} className="flex items-start gap-2 border-b pb-2 text-sm last:border-0">
                    <Badge variant="outline" className={`${meta.cls} shrink-0`}>{meta.label}</Badge>
                    <span className="text-muted-foreground shrink-0 font-mono text-xs leading-5">
                      {e.ts}
                    </span>
                    <span className="break-all leading-5">{eventBody(e)}</span>
                  </li>
                )
              })}
            </ol>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
