import type { TriageResult } from '../types'

/** A confident, fully grounded, auto-triaged result. */
export function makeResult(overrides: Partial<TriageResult> = {}): TriageResult {
  return {
    status: 'OK',
    incident_id: 'INC-TEST01',
    category: 'INFRASTRUCTURE_OUTAGE',
    priority: 'P2_HIGH',
    summary: 'The claims portal is unavailable for branch users.',
    next_action: 'Page the platform on-call engineer to restore the service.',
    evidence: ['the claims portal is down'],
    missing_information: [],
    reasoning: 'Component reported as unavailable with scope stated.',
    category_confidence: 0.9,
    priority_confidence: 0.85,
    overall_confidence: 0.85,
    grounding: { checked: 1, grounded: 1, ungrounded_spans: [] },
    routing: {
      requires_human_review: false,
      reasons: [],
      explanation: 'Auto-triaged: confidence is above threshold.',
    },
    meta: {
      correlation_id: 'abc123def456',
      provider: 'mock',
      model: 'mock-rules-v1',
      latency_ms: 42,
      attempts: 1,
      prompt_version: 'triage-prompt-v1',
      schema_version: 'triage-v1',
      input_tokens: 500,
      output_tokens: 120,
      estimated_cost_usd: 0.0001,
      redactions: {},
      warnings: [],
    },
    error: null,
    ...overrides,
  }
}
