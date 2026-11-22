/** 版本化索引覆盖段接口封装，与后端 /api/index-coverage 对齐。 */
import { request } from './client'

export type CoverageSegment = {
  segment_id: number
  segment_code: string
  scope: string
  version: number
  stage: string
  entity_count: number
  tombstone_count: number
  conflict_count: number
  entities?: string[]
  entity_generations?: Record<string, number>
  tombstones?: Record<string, number>
  conflicts: Array<Record<string, unknown>>
  deliveries: Array<Record<string, unknown>>
  published_at: string | null
  updated_at: string
}

export type DeliveryResult = {
  delivery_id: number
  build_id: string
  build_generation: number
  added: string[]
  removed: string[]
  receipt_entity_count: number
  result: string
  detail: Record<string, unknown>
  delivered_at: string
}

export type CoverageRead = {
  published: Array<Record<string, unknown>>
  ready_only: Array<Record<string, unknown>>
  published_count: number
  ready_only_count: number
  held_segments: string[]
  read_at: string
}

export type SearchResult = {
  query: string
  normalized_entity_id: string
  retrievable: boolean
  partial_ready: boolean
  message: string
  held_segments: string[]
  published_matches: Array<Record<string, unknown>>
  ready_matches: Array<Record<string, unknown>>
}

export type SupplementPlan = {
  graph_generation: number
  snapshot_entity_count: number
  covered_entity_count: number
  supplement_count: number
  stale_covered: string[]
  tasks: Array<{
    task_code: string
    entity_id: string
    graph_generation: number
    reason: string
    scope_segments: string[]
  }>
  generated_at: string
}

async function postJson<T>(path: string, payload?: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(payload ?? {}),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export const coverageApi = {
  listSegments: () => getJson<{ items: CoverageSegment[] }>('/api/index-coverage/segments'),
  getSegment: (id: number) => getJson<CoverageSegment>(`/api/index-coverage/segments/${id}`),
  registerSegment: (scope: string, segmentCode?: string) =>
    postJson<CoverageSegment>('/api/index-coverage/segments', {
      scope,
      segment_code: segmentCode || null,
    }),
  deliver: (
    segmentId: number,
    payload: {
      build_id: string
      build_generation: number
      added: string[]
      removed: string[]
      receipt_entity_count: number
      receipt_snapshot_generation: number
      graph_snapshot_generation?: number | null
    },
  ) => postJson<DeliveryResult>(`/api/index-coverage/segments/${segmentId}/deliveries`, payload),
  hold: (segmentId: number, reason: string) =>
    postJson<CoverageSegment>(`/api/index-coverage/segments/${segmentId}/hold`, { reason }),
  release: (segmentId: number) =>
    postJson<CoverageSegment>(`/api/index-coverage/segments/${segmentId}/release`),
  reconcile: (segmentId: number, graphGeneration?: number | null) =>
    postJson<Record<string, unknown>>(
      `/api/index-coverage/segments/${segmentId}/reconcile`,
      graphGeneration ? { graph_generation: graphGeneration } : {},
    ),
  publish: (segmentId: number) =>
    postJson<Record<string, unknown>>(`/api/index-coverage/segments/${segmentId}/publish`),
  readCoverage: () => getJson<CoverageRead>('/api/index-coverage/read'),
  search: (entityId: string) =>
    getJson<SearchResult>(`/api/index-coverage/search/${encodeURIComponent(entityId)}`),
  supplementPlan: (graphGeneration: number) =>
    postJson<SupplementPlan>(
      `/api/index-coverage/supplement-plan/${graphGeneration}`,
    ),
  refresh: (graphGeneration?: number | null) =>
    postJson<Record<string, unknown>>(
      '/api/index-coverage/refresh',
      graphGeneration ? { graph_generation: graphGeneration } : {},
    ),
  registerAlias: (legacyId: string, currentId: string) =>
    postJson<Record<string, unknown>>('/api/index-coverage/aliases', {
      legacy_id: legacyId,
      current_id: currentId,
    }),
  registerSnapshot: (graphGeneration: number, entityIds: string[], note: string) =>
    postJson<Record<string, unknown>>('/api/index-coverage/snapshots', {
      graph_generation: graphGeneration,
      entity_ids: entityIds,
      note,
    }),
}
