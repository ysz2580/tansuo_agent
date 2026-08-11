import { createContext, useContext } from "react"

/** 当前选中的记录分区（cohort）。
 *  null = 自动跟随最新分区（后端每次请求都解析最新，新分区出现即自动切换视图）。
 *  分区记录永不删除；选择器里可以回看任意历史分区。 */
export const CohortContext = createContext<string | null>(null)

export function useCohort(): string | null {
  return useContext(CohortContext)
}
