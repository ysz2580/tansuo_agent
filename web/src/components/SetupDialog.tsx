import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import SetupPanel from "@/components/SetupPanel"

/** 「配置 agent」子页面：控件 / 实时日志 / setup 时间线全部收进 Dialog。
 *  Agent 主屏只保留最新监督状态，不再混入 setup 的长日志。
 *  Dialog 关闭即卸载 SetupPanel → 其内部轮询自动停止。 */
export function SetupDialog({ open, onOpenChange }: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] w-[min(96vw,64rem)] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle>配置 agent（setup）</DialogTitle>
          <DialogDescription>
            对当前项目运行配置会话：阅读训练脚本 → 起草 settings 与搜索空间 → 探针验证
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          <SetupPanel />
        </div>
      </DialogContent>
    </Dialog>
  )
}
