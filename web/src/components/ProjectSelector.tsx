import { useState } from "react"
import { toast } from "sonner"
import { PlusIcon } from "lucide-react"
import { api, type ProjectCreateResp, type ProjectsResp } from "@/lib/api"
import { Button } from "@/components/ui/button"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import { NewProjectDialog } from "@/components/NewProjectDialog"

/** 项目切换器。层级语义：项目 → 分区（cohort）——切换项目会重置分区视图。
 *
 *  跨项目并行：切换项目不再被运行中的任务阻塞（各项目的搜索/配置槽相互独立），
 *  下拉项上用徽标提示哪个项目在跑。新建成功后后端已自动激活新项目。 */
export function ProjectSelector({ resp, onRefresh, onSwitched }: {
  resp: ProjectsResp | null
  onRefresh: () => void
  /** 项目实际切换成功（新建已自动激活）→ 父级重置分区选择并重挂载各页 */
  onSwitched: () => void
}) {
  const [dialogOpen, setDialogOpen] = useState(false)
  const projects = resp?.projects ?? []
  const activeId = resp?.active_id ?? ""

  const activate = async (id: string) => {
    if (id === activeId) return
    try {
      await api.activateProject(id)
      const p = projects.find((x) => x.id === id)
      toast.success(`已切换到项目「${p?.name ?? id}」`)
      onSwitched()
      onRefresh()
    } catch (e) {
      toast.error(`切换失败：${e instanceof Error ? e.message : String(e)}`)
      onRefresh()   // 下拉回显真实激活项
    }
  }

  const created = (_entry: ProjectCreateResp) => {
    onSwitched()    // register 默认已激活新项目 → 视图整体重置
    onRefresh()
  }

  return (
    <div className="flex items-center gap-2"
         title="项目 = 含训练代码与数据集的目录；tansuo 的工作产物放其中的 .tansuo/">
      <span className="text-muted-foreground text-xs">项目</span>
      <Select value={activeId || undefined} onValueChange={activate}>
        <SelectTrigger size="sm" className="max-w-60">
          <SelectValue placeholder="选择项目" />
        </SelectTrigger>
        <SelectContent>
          {projects.map((p) => (
            <SelectItem key={p.id} value={p.id}>
              <span>{p.name}</span>
              {p.run_running && (
                <span className="ml-1.5 text-[10px] text-emerald-600 dark:text-emerald-400"
                      title="该项目有搜索正在运行">▶ 搜索中</span>
              )}
              {p.setup_running && (
                <span className="ml-1.5 text-[10px] text-sky-600 dark:text-sky-400"
                      title="该项目配置 agent 正在运行">◆ 配置中</span>
              )}
              <span className="text-muted-foreground ml-1.5 font-mono text-[10px]">
                {p.dir}
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button variant="outline" size="sm" onClick={() => setDialogOpen(true)}>
        <PlusIcon className="size-3.5" />
        新建 / 打开
      </Button>
      <NewProjectDialog open={dialogOpen} onOpenChange={setDialogOpen} onCreated={created} />
    </div>
  )
}
