import { createContext, useContext } from "react"
import type { ProjectInfo } from "@/lib/api"

/** 当前激活项目。
 *
 *  项目 = 一个目录（用户训练代码 + 数据集）；tansuo 的一切相对路径
 *  （data_dir / storage / 训练脚本）与子进程 cwd 都以项目目录为基准。
 *  切换项目时视图必须整体重置：分区选择清空（跟随最新）、各页重挂载重拉数。 */
export interface ProjectContextValue {
  /** 激活项目条目；null = 尚未加载或注册表为空（后端会回退 demo/环境变量） */
  project: ProjectInfo | null
  /** 注册表变更后手动刷新列表（新建/删除后无需等轮询） */
  refresh: () => void
}

export const ProjectContext = createContext<ProjectContextValue>({
  project: null,
  refresh: () => {},
})

export function useProject(): ProjectContextValue {
  return useContext(ProjectContext)
}
