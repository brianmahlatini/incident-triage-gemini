import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TriageResultView } from './TriageResultView'
import { makeResult } from '../test/fixtures'

// These tests assert the things that would actually mislead an operator if the
// UI got them wrong. Styling is not tested; the difference between "a human
// must look at this" and "this was handled automatically" is.

describe('TriageResultView', () => {
  const threshold = 0.7

  it('shows the category and priority', () => {
    render(<TriageResultView result={makeResult()} threshold={threshold} />)
    expect(screen.getByText('INFRASTRUCTURE_OUTAGE')).toBeInTheDocument()
    expect(screen.getByText('P2_HIGH')).toBeInTheDocument()
  })

  it('announces an auto-triaged incident as auto-triaged', () => {
    render(<TriageResultView result={makeResult()} threshold={threshold} />)
    expect(screen.getByText(/AUTO-TRIAGED/)).toBeInTheDocument()
    expect(screen.queryByText(/HUMAN REVIEW REQUIRED/)).not.toBeInTheDocument()
  })

  it('announces a flagged incident and lists every reason', () => {
    // The single most important thing this component does. An incident routed
    // for review that renders as auto-triaged would silently defeat the entire
    // human-in-the-loop design.
    const result = makeResult({
      routing: {
        requires_human_review: true,
        reasons: ['HIGH_SEVERITY', 'SAFETY_OR_SECURITY_KEYWORD'],
        explanation: 'Routed for human review because it is high severity.',
      },
    })
    render(<TriageResultView result={result} threshold={threshold} />)

    expect(screen.getByText(/HUMAN REVIEW REQUIRED/)).toBeInTheDocument()
    expect(screen.getByText('HIGH_SEVERITY')).toBeInTheDocument()
    expect(screen.getByText('SAFETY_OR_SECURITY_KEYWORD')).toBeInTheDocument()
    expect(screen.queryByText(/AUTO-TRIAGED/)).not.toBeInTheDocument()
  })

  it('marks a grounded evidence span with a tick', () => {
    render(<TriageResultView result={makeResult()} threshold={threshold} />)
    expect(screen.getByText('✓')).toBeInTheDocument()
    expect(screen.getByText(/1\/1 verified against the report/)).toBeInTheDocument()
  })

  it('marks a fabricated evidence span with a cross', () => {
    // A hallucinated quote must be visually distinct. If it renders like any
    // other quote, the grounding check may as well not exist.
    const result = makeResult({
      evidence: ['a datacentre fire in Durban'],
      grounding: {
        checked: 1,
        grounded: 0,
        ungrounded_spans: ['a datacentre fire in Durban'],
      },
    })
    render(<TriageResultView result={result} threshold={threshold} />)
    expect(screen.getByText('✗')).toBeInTheDocument()
    expect(screen.getByText(/0\/1 verified against the report/)).toBeInTheDocument()
  })

  it('reports what was redacted before the model call', () => {
    const result = makeResult({
      meta: { ...makeResult().meta, redactions: { EMAIL: 2, SA_ID: 1 } },
    })
    render(<TriageResultView result={result} threshold={threshold} />)
    expect(screen.getByText(/EMAIL ×2 redacted/)).toBeInTheDocument()
    expect(screen.getByText(/SA_ID ×1 redacted/)).toBeInTheDocument()
  })

  it('lists missing information when the report was thin', () => {
    const result = makeResult({
      missing_information: ['Which system is affected', 'How many users'],
    })
    render(<TriageResultView result={result} threshold={threshold} />)
    expect(screen.getByText('Which system is affected')).toBeInTheDocument()
    expect(screen.getByText('How many users')).toBeInTheDocument()
  })

  it('renders a rejected input without crashing on absent fields', () => {
    // A REJECTED result has empty classification fields. The panel must still
    // render the error and the routing decision rather than blanking.
    const result = makeResult({
      status: 'REJECTED',
      category: 'UNKNOWN',
      priority: 'UNKNOWN',
      summary: '',
      next_action: '',
      evidence: [],
      error: 'Incident text is too short to triage.',
      routing: {
        requires_human_review: true,
        reasons: ['INPUT_QUALITY'],
        explanation: 'Rejected before triage.',
      },
    })
    render(<TriageResultView result={result} threshold={threshold} />)
    expect(screen.getByText('REJECTED')).toBeInTheDocument()
    expect(screen.getByText(/too short to triage/)).toBeInTheDocument()
    expect(screen.getByText(/HUMAN REVIEW REQUIRED/)).toBeInTheDocument()
  })

  it('surfaces the correlation id for log lookup', () => {
    render(<TriageResultView result={makeResult()} threshold={threshold} />)
    expect(screen.getByText(/abc123def456/)).toBeInTheDocument()
  })

  it('shows model warnings', () => {
    const result = makeResult({
      meta: {
        ...makeResult().meta,
        warnings: ['Summary shares little vocabulary with the report.'],
      },
    })
    render(<TriageResultView result={result} threshold={threshold} />)
    expect(screen.getByText(/shares little vocabulary/)).toBeInTheDocument()
  })
})
