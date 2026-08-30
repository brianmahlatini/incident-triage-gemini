import { useState } from 'react'

import { api, ApiError } from '../api'
import type { AppConfig, QueueEntry } from '../types'
import { priorityClass } from './TriageResultView'

// The human-in-the-loop screen.
//
// A reviewer can accept the triage as-is or override the category and
// priority. Both outcomes are recorded, not just the corrections: knowing that
// 94 of 100 flagged incidents were accepted unchanged is what tells you the
// confidence threshold is set too low and automation is being left on the
// table. Only capturing disagreements would make the system look worse the
// better it got.

function ReviewCard({
  entry,
  config,
  onDone,
}: {
  entry: QueueEntry
  config: AppConfig
  onDone: () => void
}) {
  const { result } = entry
  const [category, setCategory] = useState(result.category)
  const [priority, setPriority] = useState<string>(result.priority)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const changed = category !== result.category || priority !== result.priority

  async function submit(accepted: boolean) {
    setBusy(true)
    setError(null)
    try {
      await api.submitReview({
        incident_id: result.incident_id,
        accepted,
        corrected_category: accepted ? undefined : category,
        corrected_priority: accepted ? undefined : priority,
        reviewer_note: note,
      })
      onDone()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="queue-item">
      <div className="queue-head">
        <span className="incident-id">{result.incident_id}</span>
        <span className="badge category">{result.category}</span>
        <span className={`badge ${priorityClass(result.priority)}`}>
          {result.priority}
        </span>
        <span className="incident-id">
          conf {result.overall_confidence.toFixed(2)}
        </span>
        <span className="spacer" />
        <span className="incident-id">{entry.queued_at}</span>
      </div>

      <div className="queue-summary">{result.summary || result.error}</div>

      <div className="reason-tags" style={{ marginBottom: 10 }}>
        {result.routing.reasons.map((reason) => (
          <span key={reason} className="reason-tag">
            {reason}
          </span>
        ))}
      </div>

      {error && <div className="error-box">{error}</div>}

      <div className="review-controls">
        <select
          value={category}
          onChange={(event) => setCategory(event.target.value)}
          disabled={busy}
        >
          {config.categories.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        <select
          value={priority}
          onChange={(event) => setPriority(event.target.value)}
          disabled={busy}
        >
          {config.priorities.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        <input
          type="text"
          placeholder="Reviewer note (optional)"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          disabled={busy}
        />

        {/* Accept stays available even after an edit, so a reviewer who
            changes a dropdown by accident is not forced into recording a
            correction that did not happen. */}
        <button className="accept" onClick={() => submit(true)} disabled={busy}>
          Accept
        </button>
        <button
          className="override"
          onClick={() => submit(false)}
          disabled={busy || !changed}
          title={changed ? 'Record a correction' : 'Change a value to override'}
        >
          Override
        </button>
      </div>
    </div>
  )
}

export function ReviewQueue({
  queue,
  config,
  onRefresh,
}: {
  queue: QueueEntry[]
  config: AppConfig
  onRefresh: () => void
}) {
  if (queue.length === 0) {
    return (
      <div className="panel">
        <div className="empty">
          Nothing awaiting review. Triage an incident that trips a routing rule
          and it will appear here.
        </div>
      </div>
    )
  }

  return (
    <div>
      {queue.map((entry) => (
        <ReviewCard
          key={entry.result.incident_id}
          entry={entry}
          config={config}
          onDone={onRefresh}
        />
      ))}
      <div className="note-block">
        Every decision here is written to the log as a{' '}
        <code>triage.reviewed</code> event with the model's original answer, the
        reviewer's verdict and the prompt version in force. In production that
        stream lands in BigQuery and becomes the evaluation set — labelled
        examples drawn from live traffic rather than from a curated sample that
        stops resembling reality after a month.
      </div>
    </div>
  )
}
