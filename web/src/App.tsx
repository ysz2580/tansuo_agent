import { useCallback, useEffect, useState } from "react"
import { BookOpenIcon } from "lucide-react"
import { Toaster } from "@/components/ui/sonner"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { api, type ProjectsResp, type RunInfo, type RunStatus, type RunsResp } from "@/lib/api"
import { CohortContext } from "@/lib/cohort"
import { ProjectContext } from "@/lib/project"
import { usePolling } from "@/lib/usePolling"
import { ProjectSelector } from "@/components/ProjectSelector"
import { TutorialDialog } from "@/components/TutorialDialog"
import DashboardPage from "@/pages/DashboardPage"
import TrialsPage from "@/pages/TrialsPage"
import SpacePage from "@/pages/SpacePage"
import AgentPage from "@/pages/AgentPage"
import PromptsPage from "@/pages/PromptsPage"
import ComparePage from "@/pages/ComparePage"
import SettingsPage from "@/pages/SettingsPage"

function RunIndicator() {
  const { data } = usePolling<RunStatus>(api.runStatus, 3000)
  if (!data) return null
  return data.running ? (
    <Badge className="bg-emerald-600/15 text-emerald-700 dark:text-emerald-400">搜索运行中</Badge>
  ) : null
}

const COMPARABLE_META: Record<RunInfo["comparable"], { label: string; cls: string }> = {
  match: { label: "✔ 可比", cls: "bg-emerald-600/15 text-emerald-700 dark:text-emerald-400" },
  "code-changed": { label: "△ 代码已变", cls: "bg-amber-600/15 text-amber-700 dark:text-amber-400" },
  "data-changed": { label: "△ 数据集已变", cls: "bg-amber-600/15 text-amber-700 dark:text-amber-400" },
  "code-data-changed": { label: "△ 代码+数据集已变", cls: "bg-amber-600/15 text-amber-700 dark:text-amber-400" },
  "objective-changed": { label: "✘ 目标已变", cls: "bg-red-600/15 text-red-700 dark:text-red-400" },
  legacy: { label: "历史记录", cls: "bg-gray-600/15 text-gray-600 dark:text-gray-400" },
}

/** 记录分区选择器：记录永不删除，可回看任意历史分区。
 *  「最新分区」= 自动跟随——后端每次请求解析最新，新分区出现即自动切换视图。 */
function CohortSelector({ value, onChange }: { value: string | null; onChange: (v: string | null) => void }) {
  const { data } = usePolling<RunsResp>(api.runs, 15000)
  const runs = data?.runs ?? []
  return (
    <div className="flex items-center gap-2"
         title={data && !data.current.reliable
           ? "注意：未定位到训练脚本文件，代码指纹仅覆盖命令串（不可靠）"
           : "按训练代码/优化目标指纹自动分区；历史记录永不删除"}>
      <span className="text-muted-foreground text-xs">分区</span>
      <Select value={value ?? "latest"} onValueChange={(v) => onChange(v === "latest" ? null : v)}>
        <SelectTrigger size="sm" className="max-w-72">
          <SelectValue placeholder="最新分区" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="latest">最新分区（自动跟随）</SelectItem>
          {[...runs].reverse().map((r) => {
            const meta = COMPARABLE_META[r.comparable] ?? COMPARABLE_META.legacy
            return (
              <SelectItem key={r.id} value={r.id}>
                <span className="font-mono text-xs">{r.id}</span>
                {r.note && <span className="text-muted-foreground ml-1 text-xs">{r.note}</span>}
                <Badge variant="outline" className={`ml-1.5 text-[10px] ${meta.cls}`}>{meta.label}</Badge>
                {r.prompt_changed && (
                  <Badge variant="outline"
                         className="ml-1 text-[10px] bg-amber-600/15 text-amber-700 dark:text-amber-400">
                    △ 提示词已变
                  </Badge>
                )}
                {r.locked && <span className="text-muted-foreground ml-1 text-[10px]">(统计降级)</span>}
              </SelectItem>
            )
          })}
        </SelectContent>
      </Select>
    </div>
  )
}

export default function App() {
  const [cohort, setCohort] = useState<string | null>(null)
  const [tutorialOpen, setTutorialOpen] = useState(false)
  // 项目切换纪元：+1 → 分区重置为「跟随最新」且各页整体重挂载（按新项目重拉数）。
  // 仅 setCohort(null) 不够——cohort 本就是 null 时 key 不变，页面不会重挂载。
  const [epoch, setEpoch] = useState(0)

  // 项目注册表：慢轮询兜底 + 变更操作（新建/激活）后手动 refresh 立即回显
  const [projResp, setProjResp] = useState<ProjectsResp | null>(null)
  const refreshProjects = useCallback(() => {
    api.projects().then(setProjResp).catch(() => {})
  }, [])
  useEffect(() => {
    refreshProjects()
    const t = window.setInterval(refreshProjects, 30000)
    return () => window.clearInterval(t)
  }, [refreshProjects])
  const activeProject = projResp?.projects.find((p) => p.id === projResp.active_id) ?? null

  const onProjectSwitched = () => {
    setCohort(null)
    setEpoch((e) => e + 1)
  }
  const pageKey = `${epoch}:${cohort ?? "latest"}`

  return (
    <ProjectContext.Provider value={{ project: activeProject, refresh: refreshProjects }}>
      <CohortContext.Provider value={cohort}>
        <div className="mx-auto max-w-6xl px-4 py-6">
          <header className="mb-6 flex flex-wrap items-center gap-3">
            <h1 className="text-xl font-semibold">探索 · 智能调参</h1>
            <span className="text-muted-foreground text-sm">
              Optuna TPE 贝叶斯搜索 + LLM 监督 agent
            </span>
            <div className="ml-auto flex flex-wrap items-center gap-3">
              <Button variant="outline" size="sm" onClick={() => setTutorialOpen(true)}
                      title="从安装启动到跑完第一轮调参的完整教程">
                <BookOpenIcon className="size-3.5" /> 使用教程
              </Button>
              <ProjectSelector resp={projResp} onRefresh={refreshProjects}
                               onSwitched={onProjectSwitched} />
              <CohortSelector key={`cs-${epoch}`} value={cohort} onChange={setCohort} />
              <RunIndicator />
            </div>
          </header>

          <Tabs defaultValue="dashboard">
            <TabsList>
              <TabsTrigger value="dashboard">仪表盘</TabsTrigger>
              <TabsTrigger value="trials">试验</TabsTrigger>
              <TabsTrigger value="space">搜索空间</TabsTrigger>
              <TabsTrigger value="agent">Agent</TabsTrigger>
              <TabsTrigger value="prompts">提示词</TabsTrigger>
              <TabsTrigger value="compare">对比</TabsTrigger>
              <TabsTrigger value="settings">运行与设置</TabsTrigger>
            </TabsList>
            {/* key 随项目纪元/分区变化整体重挂载各页：轮询立即按新项目与新分区重新拉数 */}
            <TabsContent value="dashboard" className="mt-4"><DashboardPage key={pageKey} /></TabsContent>
            <TabsContent value="trials" className="mt-4"><TrialsPage key={pageKey} /></TabsContent>
            <TabsContent value="space" className="mt-4"><SpacePage key={pageKey} /></TabsContent>
            <TabsContent value="agent" className="mt-4"><AgentPage key={pageKey} /></TabsContent>
            {/* 提示词与对比跟随项目（prompts.yaml 在 settings 同目录、对比组按激活项目解析），
                随项目纪元重挂载；但不跟分区 key（提示词无分区语义、对比页自管分区选择） */}
            <TabsContent value="prompts" className="mt-4"><PromptsPage key={`p-${epoch}`} /></TabsContent>
            <TabsContent value="compare" className="mt-4"><ComparePage key={`c-${epoch}`} /></TabsContent>
            <TabsContent value="settings" className="mt-4"><SettingsPage key={pageKey} /></TabsContent>
          </Tabs>

          <Toaster richColors position="top-center" />
          <TutorialDialog open={tutorialOpen} onOpenChange={setTutorialOpen} />
        </div>
      </CohortContext.Provider>
    </ProjectContext.Provider>
  )
}
