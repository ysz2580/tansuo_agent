import { useEffect, useRef, useState } from "react"

/** 轻量轮询：每 ms 毫秒拉一次数据；组件卸载即停。fetcher 变化不会重置节奏。 */
export function usePolling<T>(fetcher: () => Promise<T>, ms: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fnRef = useRef(fetcher)
  fnRef.current = fetcher

  useEffect(() => {
    let active = true
    let timer = 0
    const tick = async () => {
      try {
        const d = await fnRef.current()
        if (active) {
          setData(d)
          setError(null)
        }
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (active) timer = window.setTimeout(tick, ms)
      }
    }
    tick()
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [ms])

  return { data, error }
}
