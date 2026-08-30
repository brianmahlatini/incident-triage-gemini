import type { TriageResult } from '../types'

// Presentation rules worth stating, because they are the point of the screen:
//
//  * The routing decision is shown first and largest. It is the only field an
//    operator must act on; category and priority are supporting detail.
//  * Confidence is drawn against the automation threshold, not as a bare
//    number. "0.62" means nothing without knowing the bar is 0.70.
//  * Evidence is marked grounded or not, per span. A fabricated quote is
//    visible as a red span rather than buried in a metrics endpoint.
//  * Redaction counts are surfaced, so it is evident what left the boundary.

export function priorityClass(priority: string): string {
  return (
    { P1_CRITICAL: 'p1', P2_HIGH: 'p2', P3_MEDIUM: 'p3', P4_LOW: 'p4' }[
      priority
    ] ?? 'unknown'
  )
}

function confidenceColour(value: number, threshold: number): string {
  if (value >= threshold) return 'var(--ok)'
  if (value >= threshold * 0.6) return 'var(--warn)'
  return 'var(--crit)'
}

function ConfidenceMeter({
  label,
  value,
  threshold,
}: {
  label: string
  value: number
  threshold: number
}) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div className="meter-legend">
        <span>{label}</span>
        <span style={{ color: confidenceColour(value, threshold) }}>
          {value.toFixed(2)}
        </span>
      </div>
      <div className="meter">
        <div
          className="meter-fill"
          style={{
            width: `${Math.max(2, value * 100)}%`,
            background: confidenceColour(value, threshold),
          }}
        />
        {/* The tick is the automation threshold: anything left of it is
            routed to a human by the confidence rule. */}
        <div
          className="meter-threshold"
          style={{ left: `${threshold * 100}%` }}
          title={`Automation threshold ${threshold}`}
        />
      </div>
    </div>
  )
}

export function TriageResultView({
  result,
  threshold,
}: {
  result: TriageResult
  threshold: number
}) {
  const review = result.routing.requires_human_review
  const redactions = Object.entries(result.meta.redactions)

  return (
    <div className="panel">
      <div className="result-head">
        <span className="incident-id">{result.incident_id}</span>
        {result.status === 'OK' ? (
          <>
            <span className="badge category">{result.category}</span>
            <span className={`badge ${priorityClass(result.priority)}`}>
              {result.priority}
            </span>
          </>
        ) : (
          <span className="badge unknown">{result.status}</span>
        )}
      </div>

      {result.error && <div className="error-box">{result.error}</div>}

      <div className={`routing ${review ? 'review' : 'auto'}`}>
        <div className="routing-title">
          {review ? '⚑ HUMAN REVIEW REQUIRED' : '✓ AUTO-TRIAGED'}
        </div>
        <div className="routing-text">{result.routing.explanation}</div>
        {result.routing.reasons.length > 0 && (
          <div className="reason-tags">
            {result.routing.reasons.map((reason) => (
              <span key={reason} className="reason-tag">
                {reason}
              </span>
            ))}
          </div>
        )}
      </div>

      {result.status === 'OK' && (
        <>
          <div className="field">
            <div className="field-label">Summary</div>
            <div className="field-value">{result.summary}</div>
          </div>

          <div className="field">
            <div className="field-label">Recommended next action</div>
            <div className="field-value">{result.next_action}</div>
          </div>

          <div className="field">
            <div className="field-label">Confidence</div>
            <ConfidenceMeter
              label="category"
              value={result.category_confidence}
              threshold={threshold}
            />
            <ConfidenceMeter
              label="priority"
              value={result.priority_confidence}
              threshold={threshold}
            />
          </div>

          {result.evidence.length > 0 && (
            <div className="field">
              <div className="field-label">
                Evidence — {result.grounding.grounded}/
                {result.grounding.checked} verified against the report
              </div>
              {result.evidence.map((span, index) => {
                const ungrounded =
                  result.grounding.ungrounded_spans.includes(span)
                return (
                  <div
                    key={index}
                    className={`evidence-item ${ungrounded ? 'ungrounded' : ''}`}
                  >
                    <span className="evidence-mark">
                      {ungrounded ? '✗' : '✓'}
                    </span>
                    <span className="evidence-text">“{span}”</span>
                  </div>
                )
              })}
            </div>
          )}

          {result.missing_information.length > 0 && (
            <div className="field">
              <div className="field-label">Missing information</div>
              {result.missing_information.map((item, index) => (
                <div key={index} className="list-item">
                  {item}
                </div>
              ))}
            </div>
          )}

          {result.reasoning && (
            <div className="field">
              <div className="field-label">Model reasoning</div>
              <div className="field-value" style={{ color: 'var(--muted)' }}>
                {result.reasoning}
              </div>
            </div>
          )}
        </>
      )}

      {result.meta.warnings.map((warning, index) => (
        <div key={index} className="warning-line">
          ⚠ {warning}
        </div>
      ))}

      <div className="meta">
        <span>
          {result.meta.provider}/{result.meta.model}
        </span>
        <span>{result.meta.latency_ms} ms</span>
        <span>
          {result.meta.attempts} attempt
          {result.meta.attempts === 1 ? '' : 's'}
        </span>
        {result.meta.input_tokens !== null && (
          <span>
            {result.meta.input_tokens}in / {result.meta.output_tokens}out tok
          </span>
        )}
        {result.meta.estimated_cost_usd !== null && (
          <span>${result.meta.estimated_cost_usd.toFixed(6)}</span>
        )}
        <span>{result.meta.prompt_version}</span>
        <span>id {result.meta.correlation_id}</span>
        {redactions.map(([kind, count]) => (
          <span key={kind} className="redaction-chip">
            {kind} ×{count} redacted
          </span>
        ))}
      </div>
    </div>
  )
}
