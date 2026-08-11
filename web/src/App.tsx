import { Toaster } from "@/components/ui/sonner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Badge } from "@/components/ui/badge"
import { api, type RunStatus } from "@/lib/api"
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

export default function App() {
  return (
    <div className="mx-auto max-w-6xl px-4 py-6">
      <header className="mb-6 flex items-center gap-3">
        <h1 className="text-xl font-semibold">探索 · 智能调参</h1>
        <span className="text-muted-foreground text-sm">
          Optuna TPE 贝叶斯搜索 + LLM 监督 agent
        </span>
        <div className="ml-auto"><RunIndicator /></div>
      </header>

      <Tabs defaultValue="dashboard">
        <TabsList>
          <TabsTrigger value="dashboard">仪表盘</TabsTrigger>
          <TabsTrigger value="trials">试验</TabsTrigger>
          <TabsTrigger value="space">搜索空间</TabsTrigger>
          <TabsTrigger value="agent">Agent</TabsTrigger>
          <TabsTrigger value="settings">运行与设置</TabsTrigger>
        </TabsList>
        <TabsContent value="dashboard" className="mt-4"><DashboardPage /></TabsContent>
        <TabsContent value="trials" className="mt-4"><TrialsPage /></TabsContent>
        <TabsContent value="space" className="mt-4"><SpacePage /></TabsContent>
        <TabsContent value="agent" className="mt-4"><AgentPage /></TabsContent>
        <TabsContent value="settings" className="mt-4"><SettingsPage /></TabsContent>
      </Tabs>

      <Toaster richColors position="top-center" />
    </div>
  )
}
