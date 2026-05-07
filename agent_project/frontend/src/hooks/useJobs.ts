import { useState, useEffect, useRef } from 'react'
import type { JobSummary } from '../types'

const POLL_INTERVAL = 4000

export function useJobs(enabled = true) {
  const [jobs, setJobs] = useState<JobSummary[]>([])
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (!enabled) return

    const fetchJobs = async () => {
      try {
        const res = await fetch('/jobs')
        if (res.ok) {
          const data = (await res.json()) as JobSummary[]
          setJobs(data)
        }
      } catch {
        // silently ignore — backend may not be up yet
      }
    }

    fetchJobs()
    timerRef.current = setInterval(fetchJobs, POLL_INTERVAL)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [enabled])

  const researchJobs = jobs.filter(j => j.intent === 'research' || j.mode === 'research')
  const runningCount = researchJobs.filter(
    j => ['classifying', 'planning', 'awaiting_approval', 'executing', 'synthesizing'].includes(j.status),
  ).length

  return { jobs, researchJobs, runningCount }
}
