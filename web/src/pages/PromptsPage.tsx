import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import {
  api,
  type PromptHistoryEntry,
  type PromptInfo,
  type PromptPreviewResp,
  type PromptsResp,
} from "@/lib/api"
import { usePolling } from "@/lib/usePolling"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"

const NAME_META: Record<string, { label: string; desc: string }> = {
  tuning_system: {
    label: "调参 · system（监督者）",
    desc: "每轮唤醒注入的监督者 system 提示词：实验背景、指标定义、搜索空间语义、工作方式与纪律。",
  },
  tuning_wake_brief: {
    label: "调参 · 每轮唤醒简报",
    desc: "每轮唤醒的开场 user 消息：轮次、已完成/剩余预算、当前空间版本。",
  },
  setup_system: {
    label: "配置 · setup（配置生成）",
    desc: "配置生成 agent 的 system 提示词：读训练脚本、推断超参数与指标、写配置并探测试验。",
  },
}

export default function PromptsPage() {
  const { data, error } = usePolling<PromptsResp>(api.prompts, 15000)

  const [selected, setSelected] = useState("tuning_system")
  const [text, setText] = useState("")
  const [rationale, setRationale] = useState("")
  const [saveOpen, setSaveOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [preview, setPreview] = useState<PromptPreviewResp | null>(null)
  const initRef = useRef(false)

  const info: PromptInfo | undefined = data?.prompts.find((p) => p.name === selected)
  const isOverride = !!info && info.override !== ""

  // 首次拿到数据后，把当前生效模板载入编辑器（之后轮询不再覆盖用户正在编辑的内容）
  useEffect(() => {
    if (!initRef.current && info) {
      initRef.current = true
      setText(info.effective)
    }
  }, [info])

  if (error) return <div className="text-red-600 py-8 text-center">加载失败：{error}</div>
  if (!data || !info) return <div className="text-muted-foreground py-12 text-center">加载中…</div>

  const switchTo = (name: string, loadText?: string) => {
    setSelected(name)
    setPreview(null)
    const target = data.prompts.find((p) => p.name === name)
    setText(loadText !== undefined ? loadText : target?.effective ?? "")
  }

  const doPreview = async () => {
    setPreviewing(true)
    try {
      setPreview(await api.promptsPreview({ which: selected, text }))
    } catch (e) {
      toast.error(`预览失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setPreviewing(false)
    }
  }

  const doSave = async (payload: { text: string; rationale: string }) => {
    setSaving(true)
    try {
      const r = await api.promptsSave({ which: selected, ...payload })
      toast.success(`已保存（版本 v${r.version}）`)
      setText(payload.text)
      setPreview(null)
    } catch (e) {
      toast.error(`保存失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setSaving(false)
    }
  }

  const restoreDefault = () => {
    setSaveOpen(false)
    void doSave({ text: "", rationale: "恢复出厂默认" })
  }

  const loadHistory = (entry: PromptHistoryEntry) => {
    switchTo(entry.which, entry.text)
    toast.info(`已载入 v${entry.version} 的文本到编辑器（尚未保存，审阅后再存）`)
  }

  const dirty = info && text !== info.effective

  return (
    <div className="space-y-4">
      <div className="text-muted-foreground flex items-center gap-2 text-sm">
        提示词版本 <span className="font-mono">v{data.version}</span>
        <span>｜空覆盖 = 用出厂默认；保存后下次 agent 唤醒即生效（全局，不分分区）</span>
      </div>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle className="text-base">提示词编辑器</CardTitle>
            <Select value={selected} onValueChange={(v) => switchTo(v)}>
              <SelectTrigger size="sm" className="max-w-72">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {data.prompts.map((p) => (
                  <SelectItem key={p.name} value={p.name}>
                    {NAME_META[p.name]?.label ?? p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Badge
              className={
                isOverride
                  ? "bg-blue-600/15 text-blue-700 dark:text-blue-300"
                  : "bg-gray-600/15 text-gray-600 dark:text-gray-400"
              }
            >
              {isOverride ? "已自定义" : "出厂默认"}
            </Badge>
            {dirty && (
              <Badge variant="outline" className="border-amber-500/50 text-amber-600 dark:text-amber-400">
                未保存
              </Badge>
            )}
          </div>
          <CardDescription>{NAME_META[selected]?.desc}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-muted-foreground text-xs">可用变量：</span>
            {info.vars.map((v) => (
              <Badge key={v} variant="secondary" className="font-mono text-xs">
                {"{{" + v + "}}"}
              </Badge>
            ))}
          </div>

          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
            className="max-h-96 min-h-40 overflow-auto font-mono text-xs"
            placeholder="留空则使用出厂默认模板；可引用上方 {{变量}} 占位符"
          />

          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={doPreview} disabled={previewing}>
              {previewing ? "预览中…" : "预览渲染"}
            </Button>
            <Button size="sm" onClick={() => { setRationale(""); setSaveOpen(true) }}>
              保存…
            </Button>
            {isOverride && (
              <Button variant="destructive" size="sm" onClick={restoreDefault} disabled={saving}>
                恢复出厂
              </Button>
            )}
          </div>

          {preview && (
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-muted-foreground text-xs">渲染预览：</span>
                {preview.missing_vars.length > 0 &&
                  preview.missing_vars.map((v) => (
                    <Badge key={v} variant="outline" className="border-amber-500/50 font-mono text-xs text-amber-600 dark:text-amber-400">
                      未填充 {"{{" + v + "}}"}
                    </Badge>
                  ))}
              </div>
              <ScrollArea className="border-muted-foreground/20 max-h-64 rounded-lg border p-2">
                <pre className="font-mono text-xs whitespace-pre-wrap">{preview.rendered}</pre>
              </ScrollArea>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">出厂默认模板（只读）</CardTitle>
          <CardDescription>恢复出厂或对照时参考；{"{{变量}}"} 会在运行时填充。</CardDescription>
        </CardHeader>
        <CardContent>
          <ScrollArea className="border-muted-foreground/20 max-h-64 rounded-lg border p-2">
            <pre className="font-mono text-xs whitespace-pre-wrap">{info.default}</pre>
          </ScrollArea>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">迭代历史（可载入回滚）</CardTitle>
          <CardDescription>
            每次保存记一条；「载入」把该版本文本放进编辑器，审阅后再保存生效。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {data.history.length === 0 ? (
            <div className="text-muted-foreground text-sm">还没有提示词编辑记录。</div>
          ) : (
            <ol className="space-y-3">
              {[...data.history].reverse().map((h, i) => (
                <li key={i} className="border-l-2 pl-3">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <Badge variant="secondary" className="font-mono">v{h.version}</Badge>
                    <Badge variant="outline" className="font-mono text-xs">{h.which}</Badge>
                    <span className="text-muted-foreground text-xs">{h.ts}</span>
                    <span className="text-muted-foreground font-mono text-xs">#{h.hash}</span>
                    <span className="text-muted-foreground text-xs">来源：{h.source}</span>
                    <Button variant="ghost" size="sm" className="ml-auto h-6 px-2 text-xs"
                            onClick={() => loadHistory(h)}>
                      载入此版本
                    </Button>
                  </div>
                  <p className="text-muted-foreground mt-1 text-xs">理由：{h.rationale}</p>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>保存提示词</DialogTitle>
            <DialogDescription>
              {NAME_META[selected]?.label ?? selected} —— rationale 必填，便于事后审计。
            </DialogDescription>
          </DialogHeader>
          <Textarea
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            placeholder="本次改动的理由（例如：发散试验偏多，强调先压 lr 上界）"
            className="min-h-20"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setSaveOpen(false)}>取消</Button>
            <Button
              disabled={saving || !rationale.trim()}
              onClick={() => {
                setSaveOpen(false)
                void doSave({ text, rationale: rationale.trim() })
              }}
            >
              {saving ? "保存中…" : "保存"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
