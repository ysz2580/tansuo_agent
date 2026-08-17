import { useEffect, useState } from "react"
import { toast } from "sonner"
import { Loader2Icon, RefreshCwIcon } from "lucide-react"
import { api, type TrainCandidate } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { DirPicker } from "@/components/DirPicker"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"

/** 训练脚本补登记 / 更换（新建项目时忘选 → 在这里补救）。
 *
 *  进入即让后端扫描项目目录：按启发式评分（文件名提示 / CLI 收参 /
 *  主入口 / 训练循环特征 / 已实现 tansuo 协议）降序列出候选，用户从
 *  列表里选；列表不中意时也可展开目录浏览手工挑。登记成功后后端会
 *  把脚手架模板的占位启动命令（path/to/your_train.py）同步回填。 */
export function TrainScriptPicker({ projectId, onDone }: {
  projectId: string
  onDone: () => void
}) {
  const [candidates, setCandidates] = useState<TrainCandidate[] | null>(null)
  const [selected, setSelected] = useState("")
  const [busy, setBusy] = useState(false)
  const [manualOpen, setManualOpen] = useState(false)
  const [manualDir, setManualDir] = useState("")
  const [manualFiles, setManualFiles] = useState<string[]>([])
  const [manualPick, setManualPick] = useState("")

  const scan = async () => {
    setCandidates(null)
    try {
      const r = await api.trainCandidates(projectId)
      setCandidates(r.candidates)
      setSelected(r.candidates[0]?.path ?? "")
      if (!r.candidates.length) setManualOpen(true)
    } catch (e) {
      toast.error(`扫描失败：${e instanceof Error ? e.message : String(e)}`)
      setCandidates([])
    }
  }

  useEffect(() => { void scan() }, [projectId])  // eslint-disable-line react-hooks/exhaustive-deps

  // 手工浏览：目录变化 → 拉该目录 .py 文件
  useEffect(() => {
    if (!manualDir) { setManualFiles([]); return }
    let active = true
    api.browseFiles(manualDir)
      .then((r) => { if (active) { setManualFiles(r.files); setManualPick(r.files[0] ?? "") } })
      .catch(() => { if (active) setManualFiles([]) })
    return () => { active = false }
  }, [manualDir])

  const register = async (path: string) => {
    if (!path) return
    setBusy(true)
    try {
      const r = await api.setTrainScript(projectId, path)
      toast.success(r.settings_patched
        ? "已登记训练脚本，并同步回填了 settings.yaml 的占位启动命令"
        : "已登记训练脚本")
      onDone()
    } catch (e) {
      toast.error(`登记失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-2 space-y-2 rounded-md border p-3">
      <div className="flex items-center gap-2 text-sm">
        <span className="font-medium">选择主训练脚本</span>
        <Button variant="ghost" size="sm" className="h-6 px-2 text-xs"
                onClick={() => void scan()} disabled={busy}>
          <RefreshCwIcon className="size-3" /> 重新扫描
        </Button>
      </div>

      {candidates === null ? (
        <p className="text-muted-foreground flex items-center gap-2 text-xs">
          <Loader2Icon className="size-3 animate-spin" /> 正在扫描项目目录…
        </p>
      ) : candidates.length > 0 ? (
        <div className="space-y-1">
          {candidates.map((c) => (
            <button key={c.path} type="button"
                    onClick={() => setSelected(c.path)}
                    className={`flex w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md border px-2 py-1.5 text-left text-xs transition-colors ${
                      selected === c.path
                        ? "border-primary bg-primary/5"
                        : "hover:bg-muted/50"}`}>
              <span className={`size-2 shrink-0 rounded-full ${
                selected === c.path ? "bg-primary" : "bg-muted-foreground/30"}`} />
              <span className="font-mono">{c.rel}</span>
              {c.reasons.map((r) => (
                <Badge key={r} variant="outline" className="text-[10px] font-normal">
                  {r}
                </Badge>
              ))}
            </button>
          ))}
        </div>
      ) : (
        <p className="text-muted-foreground text-xs">
          未找到像训练脚本的 .py 文件，请在下方目录浏览里手工选择。
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {candidates !== null && candidates.length > 0 && (
          <Button size="sm" disabled={busy || !selected}
                  onClick={() => void register(selected)}>
            {busy && <Loader2Icon className="size-3.5 animate-spin" />}
            登记所选脚本
          </Button>
        )}
        <Button variant="outline" size="sm"
                onClick={() => setManualOpen((v) => !v)}>
          {manualOpen ? "收起目录浏览" : "浏览目录手工选"}
        </Button>
      </div>

      {manualOpen && (
        <div className="space-y-2">
          <DirPicker value={manualDir} onChange={setManualDir} />
          {manualDir && (manualFiles.length > 0 ? (
            <div className="flex items-center gap-2">
              <Select value={manualPick} onValueChange={setManualPick}>
                <SelectTrigger size="sm" className="max-w-72">
                  <SelectValue placeholder="选择 .py 脚本" />
                </SelectTrigger>
                <SelectContent>
                  {manualFiles.map((f) => (
                    <SelectItem key={f} value={f}>
                      <span className="font-mono text-xs">{f}</span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button size="sm" variant="outline" disabled={busy || !manualPick}
                      onClick={() => void register(`${manualDir}/${manualPick}`)}>
                登记
              </Button>
            </div>
          ) : (
            <p className="text-muted-foreground text-xs">该目录下没有 .py 文件</p>
          ))}
        </div>
      )}
    </div>
  )
}
