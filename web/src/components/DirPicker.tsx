import { useEffect, useState } from "react"
import { ChevronRightIcon, FolderIcon, HardDriveIcon, Loader2Icon } from "lucide-react"
import { api, type BrowseResp } from "@/lib/api"
import { Button } from "@/components/ui/button"

/** 服务端目录浏览器（浏览器无法枚举服务器文件夹）：只列目录、点击进入。
 *  value 为当前浏览路径；"" = 根视图（Windows 盘符列表 / 其他平台 home）。
 *  受控组件：选中由父级 onChange 接管（「当前浏览到的目录」即候选项目目录）。 */
export function DirPicker({ value, onChange }: {
  value: string
  onChange: (path: string) => void
}) {
  const [data, setData] = useState<BrowseResp | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let active = true
    setLoading(true)
    api.browseDir(value)
      .then((d) => { if (active) { setData(d); setError(null) } })
      .catch((e) => { if (active) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [value])

  return (
    <div className="rounded-md border">
      <div className="flex items-center gap-1 border-b px-2 py-1.5">
        {data?.parent != null && (
          <Button variant="ghost" size="sm" className="h-6 px-2 text-xs"
                  onClick={() => onChange(data.parent as string)}>
            ↑ 上级
          </Button>
        )}
        <span className="text-muted-foreground truncate font-mono text-xs"
              title={data?.path ?? ""}>
          {data?.path || "（选择磁盘）"}
        </span>
        {loading && (
          <Loader2Icon className="text-muted-foreground ml-auto size-3.5 shrink-0 animate-spin" />
        )}
      </div>
      <div className="max-h-52 overflow-y-auto p-1">
        {error && <div className="p-2 text-xs text-red-600">加载失败：{error}</div>}
        {data?.dirs.map((d) => (
          <button key={d.path} type="button"
                  className="hover:bg-accent flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-sm"
                  onClick={() => onChange(d.path)}>
            {value === ""
              ? <HardDriveIcon className="size-4 shrink-0" />
              : <FolderIcon className="size-4 shrink-0" />}
            <span className="truncate">{d.name}</span>
            {d.has_children && (
              <ChevronRightIcon className="text-muted-foreground ml-auto size-3.5 shrink-0" />
            )}
          </button>
        ))}
        {data && data.dirs.length === 0 && !error && (
          <div className="text-muted-foreground p-2 text-xs">无子目录</div>
        )}
      </div>
    </div>
  )
}
