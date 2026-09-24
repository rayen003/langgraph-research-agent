import { useCallback, useEffect, useState } from 'react'
import type { CollaborationActor, CollaborationAssignment, CollaborationChannel, CollaborationMention, CollaborationMessage, CollaborationNotification } from '../types'

export function useCollaboration(workspaceId: string) {
  const [actors, setActors] = useState<CollaborationActor[]>([])
  const [channels, setChannels] = useState<CollaborationChannel[]>([])
  const [notifications, setNotifications] = useState<CollaborationNotification[]>([])
  const [messages, setMessages] = useState<Record<string, CollaborationMessage[]>>({})
  const [assignments, setAssignments] = useState<CollaborationAssignment[]>([])

  const refresh = useCallback(async () => {
    await fetch(`/collaboration/workspaces/${encodeURIComponent(workspaceId)}`, { method: 'POST' })
    const [actorRes, channelRes, notificationRes, assignmentRes] = await Promise.all([
      fetch(`/collaboration/workspaces/${encodeURIComponent(workspaceId)}/actors`),
      fetch(`/collaboration/workspaces/${encodeURIComponent(workspaceId)}/channels`),
      fetch('/collaboration/actors/human%3Alocal/notifications'),
      fetch(`/collaboration/workspaces/${encodeURIComponent(workspaceId)}/assignments`),
    ])
    if (actorRes.ok) setActors(await actorRes.json())
    if (channelRes.ok) setChannels(await channelRes.json())
    if (notificationRes.ok) setNotifications(await notificationRes.json())
    if (assignmentRes.ok) setAssignments(await assignmentRes.json())
  }, [workspaceId])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => { const timer = window.setInterval(() => void refresh(), 10000); return () => window.clearInterval(timer) }, [refresh])
  useEffect(() => {
    const heartbeat = () => { void fetch('/collaboration/actors/human%3Alocal/presence', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'available' }) }) }
    heartbeat()
    const timer = window.setInterval(heartbeat, 20000)
    return () => window.clearInterval(timer)
  }, [])

  const loadMessages = useCallback(async (channelId: string) => {
    const response = await fetch(`/collaboration/channels/${encodeURIComponent(channelId)}/messages`)
    if (response.ok) {
      const items = await response.json() as CollaborationMessage[]
      setMessages(prev => ({ ...prev, [channelId]: items }))
    }
  }, [])

  const createChannel = useCallback(async (name: string, kind: CollaborationChannel['kind'] = 'channel') => {
    const response = await fetch(`/collaboration/workspaces/${encodeURIComponent(workspaceId)}/channels`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, kind, actor_id: 'human:local' }),
    })
    if (!response.ok) return null
    const channel = await response.json() as CollaborationChannel
    setChannels(prev => [channel, ...prev])
    return channel
  }, [workspaceId])

  const sendMessage = useCallback(async (channelId: string, body: string, mentions: CollaborationMention[], objectVersionIds: string[]) => {
    const response = await fetch(`/collaboration/workspaces/${encodeURIComponent(workspaceId)}/channels/${encodeURIComponent(channelId)}/messages`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body, actor_id: 'human:local', mentions, object_version_ids: objectVersionIds }),
    })
    if (!response.ok) return null
    const message = await response.json() as CollaborationMessage
    setMessages(prev => ({ ...prev, [channelId]: [...(prev[channelId] ?? []), message] }))
    if (message.assignment) {
      setAssignments(prev => [message.assignment!, ...prev.filter(item => item.assignment_id !== message.assignment!.assignment_id)])
    }
    return message
  }, [workspaceId])

  const markRead = useCallback(async (notificationId: string) => {
    const response = await fetch(`/collaboration/notifications/${encodeURIComponent(notificationId)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ read: true }),
    })
    if (response.ok) setNotifications(prev => prev.map(item => item.notification_id === notificationId ? { ...item, read: true } : item))
  }, [])

  const ingestMessage = useCallback((message: CollaborationMessage) => {
    setMessages(prev => {
      const current = prev[message.channel_id] ?? []
      if (current.some(item => item.message_id === message.message_id)) return prev
      return { ...prev, [message.channel_id]: [...current, message] }
    })
  }, [])

  const updateAssignment = useCallback(async (assignmentId: string, status: CollaborationAssignment['status']) => {
    const response = await fetch(`/collaboration/assignments/${encodeURIComponent(assignmentId)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status, actor_id: 'human:local' }) })
    if (!response.ok) return
    const updated = await response.json() as CollaborationAssignment
    setAssignments(prev => prev.map(item => item.assignment_id === assignmentId ? updated : item))
  }, [])

  return { actors, channels, notifications, assignments, messages, refresh, loadMessages, createChannel, sendMessage, markRead, ingestMessage, updateAssignment }
}
