// Mirrors src/triage/schema.py. The Python model is the source of truth; these
// types exist so the UI cannot quietly read a field the API does not send.
//
// In a longer-lived project this file would be generated from the OpenAPI
// schema FastAPI already publishes at /openapi.json, rather than hand-written.
// At this size, generation is more machinery than the drift risk justifies.

export type Status = 'OK' | 'REJECTED' | 'FAILED'

export type Priority =
  | 'P1_CRITICAL'
  | 'P2_HIGH'
  | 'P3_MEDIUM'
  | 'P4_LOW'
  | 'UNKNOWN'

export interface GroundingReport {
  checked: number
  grounded: number
  ungrounded_spans: string[]
}

export interface RoutingDecision {
  requires_human_review: boolean
  reasons: string[]
  explanation: string
}

export interface TriageMeta {
  correlation_id: string
  provider: string
  model: string
  latency_ms: number
  attempts: number
  prompt_version: string
  schema_version: string
  input_tokens: number | null
  output_tokens: number | null
  estimated_cost_usd: number | null
  redactions: Record<string, number>
  warnings: string[]
}

export interface TriageResult {
  status: Status
  incident_id: string
  category: string
  priority: Priority
  summary: string
  next_action: string
  evidence: string[]
  missing_information: string[]
  reasoning: string
  category_confidence: number
  priority_confidence: number
  overall_confidence: number
  grounding: GroundingReport
  routing: RoutingDecision
  meta: TriageMeta
  error: string | null
}

export interface Sample {
  id: string
  label: string
  demonstrates: string
  text: string
}

export interface QueueEntry {
  queued_at: string
  result: TriageResult
}

export interface Metrics {
  total: number
  ok: number
  rejected: number
  failed: number
  review_required: number
  auto_triaged: number
  review_rate: number
  failure_rate: number
  retries: number
  latency_p50_ms: number
  latency_p95_ms: number
  by_category: Record<string, number>
  by_priority: Record<string, number>
  by_review_reason: Record<string, number>
  total_input_tokens: number
  total_output_tokens: number
  estimated_cost_usd: number
  queue_depth: number
  reviewed: number
  reviewer_agreement_rate?: number
}

export interface AppConfig {
  categories: string[]
  priorities: Priority[]
  confidence_threshold: number
  provider: string
  model: string
}
