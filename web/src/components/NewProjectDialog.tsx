import { useEffect, useState } from "react"
import { toast } from "sonner"
import { api, type ProjectCreateResp } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { DirPicker } from "@/components/DirPicker"
import { Loader2Icon } from "lucide-react"

const NONE = "__none__"

/** 新建 / 打开项目对话框。
 *
 *  「项目目录」= 用户训练代码与数据集所在目录；tansuo 在其中脚手架 `.tansuo/`
 *  （settings/search_space/data/runs），不碰用户原文件。目录已含
 *  `.tansuo/settings.yaml` 时后端直接按「打开既有项目」注册（scaffolded=false）。 */
export function NewProjectDialog({ open, onOpenChange, onCreated }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (entry: ProjectCreateResp) => void
}) {
  const [name, setName] = useState("")
  const [dir, setDir] = useState("")
  const [pyFiles, setPyFiles] = useState<string[]>([])
  const [script, setScript] = useState<string>(NONE)
  const [busy, setBusy] = useState(false)

  // 打开时重置表单
  useEffect(() => {
    if (open) {
      setName("")
      setDir("")
      setPyFiles([])
      setScript(NONE)
      setBusy(false)
    }
  }, [open])

  // 目录变化 → 拉该目录下的 .py 文件供选择主训练脚本
  useEffect(() => {
    let active = true
    if (!dir) {
      setPyFiles([])
      return
    }
    api.browseFiles(dir)
      .then((r) => { if (active) { setPyFiles(r.files); setScript(NONE) } })
      .catch(() => { if (active) setPyFiles([]) })
    return () => { active = false }
  }, [dir])

  const submit = async () => {
    if (!dir) {
      toast.error("请先在项目目录里浏览并选中一个目录")
      return
    }
    setBusy(true)
    try {
      const body = {
        name: name.trim() || undefined,
        dir,
        train_script: script !== NONE ? `${dir}/${script}` : undefined,
      }
      const entry = await api.createProject(body)
      toast.success(entry.scaffolded
        ? `已创建项目「${entry.name}」并生成 .tansuo/ 脚手架`
        : `已打开既有项目「${entry.name}」（检测到 .tansuo/）`)
      onOpenChange(false)
      onCreated(entry)
    } catch (e) {
      toast.error(`创建失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>新建 / 打开项目</DialogTitle>
          <DialogDescription>
            选中包含数据集与主训练代码的目录。tansuo 会在其中建立 .tansuo/
            工作子目录（配置 / 搜索空间 / 运行记录），你的原文件保持原位。
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="grid gap-1.5">
            <Label htmlFor="proj-name">项目名（可选，缺省用目录名）</Label>
            <Input id="proj-name" value={name} placeholder="my_experiment"
                   onChange={(e) => setName(e.target.value)} />
          </div>

          <div className="grid gap-1.5">
            <Label>项目目录（浏览并点击选中）</Label>
            <DirPicker value={dir} onChange={setDir} />
            {dir && (
              <p className="text-muted-foreground text-xs">
                已选中：<span className="font-mono">{dir}</span>
              </p>
            )}
          </div>

          <div className="grid gap-1.5">
            <Label>主训练脚本（供配置 agent 阅读；可稍后再配）</Label>
            <Select value={script} onValueChange={setScript} disabled={!dir}>
              <SelectTrigger size="sm">
                <SelectValue placeholder={dir ? "选择 .py 脚本" : "先选择项目目录"} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>暂不指定</SelectItem>
                {pyFiles.map((f) => (
                  <SelectItem key={f} value={f}>
                    <span className="font-mono text-xs">{f}</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
          <Button onClick={submit} disabled={busy || !dir}>
            {busy && <Loader2Icon className="size-4 animate-spin" />}
            创建 / 打开
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
