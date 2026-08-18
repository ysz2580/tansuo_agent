import { useEffect, useState, type ReactNode } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Loader2Icon } from "lucide-react"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

/** 教程 markdown 的排版映射：无 @tailwindcss/typography 插件，
 *  逐元素给 class（含暗色模式），保证代码块/表格/引用块渲染质感。 */
const MD_COMPONENTS = {
  h1: (p: { children?: ReactNode }) => (
    <h1 className="mt-2 mb-4 border-b pb-2 text-xl font-bold first:mt-0">{p.children}</h1>
  ),
  h2: (p: { children?: ReactNode }) => (
    <h2 className="mt-8 mb-3 border-b pb-1.5 text-lg font-semibold">{p.children}</h2>
  ),
  h3: (p: { children?: ReactNode }) => (
    <h3 className="mt-6 mb-2 text-base font-semibold">{p.children}</h3>
  ),
  p: (p: { children?: ReactNode }) => (
    <p className="my-2.5 leading-7">{p.children}</p>
  ),
  ul: (p: { children?: ReactNode }) => (
    <ul className="my-2 list-disc space-y-1 pl-6">{p.children}</ul>
  ),
  ol: (p: { children?: ReactNode }) => (
    <ol className="my-2 list-decimal space-y-1 pl-6">{p.children}</ol>
  ),
  li: (p: { children?: ReactNode }) => <li className="leading-7">{p.children}</li>,
  a: (p: { href?: string; children?: ReactNode }) => (
    <a href={p.href} className="text-primary underline underline-offset-2">{p.children}</a>
  ),
  strong: (p: { children?: ReactNode }) => (
    <strong className="font-semibold">{p.children}</strong>
  ),
  blockquote: (p: { children?: ReactNode }) => (
    <blockquote className="text-muted-foreground my-3 border-l-4 pl-3">{p.children}</blockquote>
  ),
  hr: () => <hr className="my-6" />,
  table: (p: { children?: ReactNode }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-sm">{p.children}</table>
    </div>
  ),
  thead: (p: { children?: ReactNode }) => (
    <thead className="bg-muted/60">{p.children}</thead>
  ),
  th: (p: { children?: ReactNode }) => (
    <th className="border px-3 py-1.5 text-left font-semibold">{p.children}</th>
  ),
  td: (p: { children?: ReactNode }) => (
    <td className="border px-3 py-1.5 align-top leading-6">{p.children}</td>
  ),
  pre: (p: { children?: ReactNode }) => (
    <pre className="bg-muted/60 my-3 overflow-x-auto rounded-md p-3 font-mono text-[13px] leading-6">
      {p.children}
    </pre>
  ),
  code: (p: { children?: ReactNode }) => {
    const text = String(p.children ?? "")
    if (text.includes("\n")) return <code>{p.children}</code>   // 块级：交给 pre 排版
    return (
      <code className="bg-muted rounded px-1.5 py-0.5 font-mono text-[13px]">
        {p.children}
      </code>
    )
  },
}

/** 「使用教程」对话框：拉后端 /api/docs/tutorial 的 markdown 原文并渲染。
 *  内容单一事实来源在 docs/tutorial-getting-started.md，改文档即改界面。 */
export function TutorialDialog({ open, onOpenChange }: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [markdown, setMarkdown] = useState<string | null>(null)
  const [error, setError] = useState("")

  useEffect(() => {
    if (!open || markdown !== null || error) return
    api.tutorialDoc()
      .then((r) => setMarkdown(r.markdown))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [open, markdown, error])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[88vh] w-[min(96vw,56rem)] flex-col gap-0 p-0">
        <DialogHeader className="border-b px-6 py-4">
          <DialogTitle>从零开始使用教程</DialogTitle>
          <DialogDescription>
            从安装启动到跑完第一轮智能调参；源文件 docs/tutorial-getting-started.md
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {markdown ? (
            <article className="text-sm" data-tutorial>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                {markdown}
              </ReactMarkdown>
            </article>
          ) : error ? (
            <div className="space-y-2 py-8 text-center text-sm">
              <p className="text-red-600 dark:text-red-400">教程加载失败：{error}</p>
              <Button variant="outline" size="sm" onClick={() => { setError(""); setMarkdown(null) }}>
                重试
              </Button>
            </div>
          ) : (
            <div className="text-muted-foreground flex items-center justify-center gap-2 py-16 text-sm">
              <Loader2Icon className="size-4 animate-spin" /> 加载教程…
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
