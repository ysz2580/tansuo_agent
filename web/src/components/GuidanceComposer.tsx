import { useState } from "react"
import { MegaphoneIcon } from "lucide-react"
import { toast } from "sonner"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"

/** 人→agent 指令通道：搜索运行中排队（guidance.jsonl），下一轮 agent 唤醒
 *  注入 wake brief（journal 留 AGENT_GUIDANCE 审计）。空闲时后端拒收——
 *  指令的生命周期依附运行中的会话，无唤醒则无人消费。 */
export function GuidanceComposer({ running }: { running: boolean }) {
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    const t = text.trim()
    if (!t || busy) return
    setBusy(true)
    try {
      await api.guidance({ text: t })
      toast.success("指令已排队：下一轮 agent 唤醒时注入")
      setText("")
    } catch (e) {
      toast.error(`发送失败：${e instanceof Error ? e.message : e}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardContent className="space-y-2 pt-6">
        <div className="flex items-center gap-2 text-sm font-medium">
          <MegaphoneIcon className="size-4 text-cyan-600 dark:text-cyan-400" />
          给监督 agent 下指令
        </div>
        <Textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={!running || busy}
          rows={2}
          placeholder={running
            ? "例如：重点探更小的 lr / 先别再动搜索空间，把预算留给自定义试验……"
            : "搜索运行时才能下达指令（下一轮 agent 唤醒时注入）"}
        />
        <div className="flex items-center justify-end gap-2">
          <span className="text-muted-foreground text-xs">
            {running ? "发送后排队，下一轮唤醒消费并写 journal 审计" : "搜索未运行"}
          </span>
          <Button size="sm" onClick={submit} disabled={!running || busy || !text.trim()}>
            发送
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
