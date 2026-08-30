// All network access lives here.
//
// The point is error discipline: every call goes through one helper that turns
// a non-2xx response into a typed Error with the server's own detail message
// attached. Scattering fetch calls through components is how a UI ends up
// showing a blank panel when the backend returns a 422 - the request failed,
// nothing threw, and the component rendered undefined.

import type {
  AppConfig,
  Metrics,
  QueueEntry,
  Sample,
  TriageResult,
} from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    // A transport failure is the most common thing a reviewer will hit while
    // trying this out, so it gets a message that says what to do about it.
    throw new ApiError(
      'Cannot reach the triage API. Is the backend running on port 8000?',
      0,
    )
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') {
        detail = body.detail
      } else if (Array.isArray(body.detail) && body.detail.length > 0) {
        // FastAPI validation errors arrive as a list of field-level problems.
        detail = body.detail
          .map((item: { loc?: string[]; msg?: string }) =>
            [item.loc?.slice(1).join('.'), item.msg].filter(Boolean).join(': '),
          )
          .join('; ')
      }
    } catch {
      // Response had no JSON body; the status-based message stands.
    }
    throw new ApiError(detail, response.status)
  }

  return response.json() as Promise<T>
}

export const api = {
  config: () => request<AppConfig>('/api/config'),
  samples: () => request<Sample[]>('/api/samples'),
  metrics: () => request<Metrics>('/api/metrics'),
  reviewQueue: () => request<QueueEntry[]>('/api/review-queue'),
  recent: () => request<TriageResult[]>('/api/recent?limit=20'),

  triage: (text: string, incidentId?: string) =>
    request<TriageResult>('/api/triage', {
      method: 'POST',
      body: JSON.stringify({ text, incident_id: incidentId ?? null }),
    }),

  submitReview: (payload: {
    incident_id: string
    accepted: boolean
    corrected_category?: string
    corrected_priority?: string
    reviewer_note?: string
  }) =>
    request<{ status: string; queue_depth: number }>(
      '/api/review-queue/decision',
      { method: 'POST', body: JSON.stringify(payload) },
    ),
}
