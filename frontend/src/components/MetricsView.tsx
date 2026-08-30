import type { Metrics } from '../types'

// The four numbers on top are the ones that would go on an operations
// dashboard. Each is paired with a note saying what it means and when to worry,
// because a dashboard nobody can interpret is a dashboard nobody acts on.

function Card({
  value,
  label,
  note,
  colour,
}: {
  value: string
  label: string
  note: string
  colour?: string
}) {
  return (
    <div className="metric-card">
      <div className="metric-value" style={colour ? { color: colour } : undefined}>
        {value}
      </div>
      <div className="metric-label">{label}</div>
      <div className="metric-note">{note}</div>
    </div>
  )
}

function BarChart({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) {
    return <div className="empty">No data yet.</div>
  }
  const max = Math.max(...entries.map(([, count]) => count))
  return (
    <div>
      {entries.map(([label, count]) => (
        <div key={label} className="bar-row">
          <div className="bar-label" title={label}>
            {label}
          </div>
          <div className="bar-track">
            <div
              className="bar-fill"
              style={{ width: `${(count / max) * 100}%` }}
            />
          </div>
          <div className="bar-value">{count}</div>
        </div>
      ))}
    </div>
  )
}

export function MetricsView({ metrics }: { metrics: Metrics }) {
  if (metrics.total === 0) {
    return (
      <div className="panel">
        <div className="empty">
          No incidents triaged yet in this session. Run a few from the Triage
          tab.
        </div>
      </div>
    )
  }

  const reviewRate = metrics.review_rate

  return (
    <div>
      <div className="metric-grid">
        <Card
          value={String(metrics.total)}
          label="Incidents triaged"
          note="This process instance only; counters reset on restart."
        />
        <Card
          value={`${(reviewRate * 100).toFixed(0)}%`}
          label="Human review rate"
          note="Rising means the model got less certain or inputs got worse. Falling below plan means the threshold may be too loose."
          colour={
            reviewRate > 0.6
              ? 'var(--warn)'
              : reviewRate < 0.1
                ? 'var(--crit)'
                : 'var(--ok)'
          }
        />
        <Card
          value={`${metrics.latency_p95_ms} ms`}
          label="p95 latency"
          note="p95 rather than the mean: the tail is what breaks an SLA."
        />
        <Card
          value={`$${metrics.estimated_cost_usd.toFixed(4)}`}
          label="Estimated spend"
          note={`${metrics.total_input_tokens.toLocaleString()} in / ${metrics.total_output_tokens.toLocaleString()} out tokens.`}
        />
        <Card
          value={String(metrics.queue_depth)}
          label="Awaiting review"
          note="Queue depth is the capacity signal: sustained growth means review is the bottleneck, not the model."
          colour={metrics.queue_depth > 10 ? 'var(--warn)' : undefined}
        />
        <Card
          value={`${(metrics.failure_rate * 100).toFixed(1)}%`}
          label="Failure rate"
          note={`${metrics.failed} failed, ${metrics.rejected} rejected at input, ${metrics.retries} retries.`}
          colour={metrics.failure_rate > 0.02 ? 'var(--crit)' : 'var(--ok)'}
        />
        {metrics.reviewer_agreement_rate !== undefined && (
          <Card
            value={`${(metrics.reviewer_agreement_rate * 100).toFixed(0)}%`}
            label="Reviewer agreement"
            note={`Of ${metrics.reviewed} reviewed, how many were accepted unchanged. The one quality signal that needs no labelled dataset.`}
            colour={
              metrics.reviewer_agreement_rate < 0.7 ? 'var(--warn)' : 'var(--ok)'
            }
          />
        )}
      </div>

      <div className="columns">
        <div className="panel">
          <h2>By category</h2>
          <p className="hint">
            A sudden shift in this distribution is an early warning: either the
            estate changed or the model's behaviour did.
          </p>
          <BarChart data={metrics.by_category} />
        </div>

        <div className="panel">
          <h2>By priority</h2>
          <p className="hint">
            Worth watching for drift towards P1. Priority inflation is the
            classic slow failure of an automated triage system.
          </p>
          <BarChart data={metrics.by_priority} />
        </div>
      </div>

      <div className="panel" style={{ marginTop: 20 }}>
        <h2>Why incidents were sent to a human</h2>
        <p className="hint">
          The actionable breakdown. A spike in UNGROUNDED_EVIDENCE points at the
          model; a spike in INPUT_QUALITY points at whatever is submitting the
          reports.
        </p>
        <BarChart data={metrics.by_review_reason} />
      </div>

      <div className="note-block">
        These counters are in-process and per-instance, which is honest for a
        proof of concept and wrong for production. The same measurements would
        be written as Cloud Monitoring custom metrics, or derived from the
        BigQuery results table, so they survive restarts and aggregate across
        instances. The metric names here match the intended production ones, so
        the dashboards carry over.
      </div>
    </div>
  )
}
