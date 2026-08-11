import { useState } from "react"
import { Toaster } from "@/components/ui/sonner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Badge } from "@/components/ui/badge"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { api, type RunInfo, type RunStatus, type RunsResp } from "@/lib/api"
import { CohortContext } from "@/lib/cohort"
import { usePolling } from "@/lib/usePolling"
import DashboardPage from "@/pages/DashboardPage"
import TrialsPage from "@/pages/TrialsPage"
import SpacePage from "@/pages/SpacePage"
import AgentPage from "@/pages/AgentPage"
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

  return (
    <CohortContext.Provider value={cohort}>
      <div className="mx-auto max-w-6xl px-4 py-6">
        <header className="mb-6 flex items-center gap-3">
          <h1 className="text-xl font-semibold">探索 · 智能调参</h1>
          <span className="text-muted-foreground text-sm">
            Optuna TPE 贝叶斯搜索 + LLM 监督 agent
          </span>
          <div className="ml-auto flex items-center gap-3">
            <CohortSelector value={cohort} onChange={setCohort} />
            <RunIndicator />
          </div>
        </header>

        <Tabs defaultValue="dashboard">
          <TabsList>
            <TabsTrigger value="dashboard">仪表盘</TabsTrigger>
            <TabsTrigger value="trials">试验</TabsTrigger>
            <TabsTrigger value="space">搜索空间</TabsTrigger>
            <TabsTrigger value="agent">Agent</TabsTrigger>
            <TabsTrigger value="settings">运行与设置</TabsTrigger>
          </TabsList>
          {/* key 随分区变化整体重挂载各页：轮询立即按新分区重新拉数 */}
          <TabsContent value="dashboard" className="mt-4"><DashboardPage key={cohort ?? "latest"} /></TabsContent>
          <TabsContent value="trials" className="mt-4"><TrialsPage key={cohort ?? "latest"} /></TabsContent>
          <TabsContent value="space" className="mt-4"><SpacePage key={cohort ?? "latest"} /></TabsContent>
          <TabsContent value="agent" className="mt-4"><AgentPage key={cohort ?? "latest"} /></TabsContent>
          <TabsContent value="settings" className="mt-4"><SettingsPage key={cohort ?? "latest"} /></TabsContent>
        </Tabs>

        <Toaster richColors position="top-center" />
      </div>
    </CohortContext.Provider>
  )
}
