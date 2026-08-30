import { useCallback, useEffect, useState } from 'react'

import { api, ApiError } from './api'
import { MetricsView } from './components/MetricsView'
import { ReviewQueue } from './components/ReviewQueue'
import { TriageResultView } from './components/TriageResultView'
import type {
  AppConfig,
  Metrics,
  QueueEntry,
  Sample,
  TriageResult,
} from './types'

type Tab = 'triage' | 'queue' | 'metrics'

// The taxonomy, the priorities and the confidence threshold all come from
// /api/config rather than being duplicated here. A frontend that keeps its own
// copy of the category list is a frontend that silently disagrees with the
// backend the first time the list changes.

export default function App() {
  const [tab, setTab] = useState<Tab>('triage')
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [samples, setSamples] = useState<Sample[]>([])
  const [text, setText] = useState('')
  const [activeSample, setActiveSample] = useState<Sample | null>(null)
  const [result, setResult] = useState<TriageResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [queue, setQueue] = useState<QueueEntry[]>([])
  const [metrics, setMetrics] = useState<Metrics | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [nextQueue, nextMetrics] = await Promise.all([
        api.reviewQueue(),
        api.metrics(),
      ])
      setQueue(nextQueue)
      setMetrics(nextMetrics)
    } catch {
      // A failed background refresh must not clear the screen the user is
      // reading. The next successful poll repairs it.
    }
  }, [])

  useEffect(() => {
    Promise.all([api.config(), api.samples()])
      .then(([nextConfig, nextSamples]) => {
        setConfig(nextConfig)
        setSamples(nextSamples)
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : String(err)),
      )
    void refresh()
  }, [refresh])

  async function submit() {
    if (!text.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const next = await api.triage(text)
      setResult(next)
      void refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      setResult(null)
    } finally {
      setBusy(false)
    }
  }

  function pickSample(sample: Sample) {
    setText(sample.text)
    setActiveSample(sample)
    setResult(null)
    setError(null)
  }

  const live = config?.provider !== 'mock'

  return (
    <div className="app">
      <header className="masthead">
        <div>
          <h1>Incident Triage Console</h1>
          <p>
            First-stage triage for operational incidents — Gemini proposes, the
            routing rules decide, a human confirms anything that matters.
          </p>
        </div>
        <span className={`provider-chip ${live ? 'live' : ''}`}>
          {config ? `${config.provider} · ${config.model}` : 'connecting…'}
        </span>
      </header>

      <nav className="tabs">
        <button
          className={tab === 'triage' ? 'active' : ''}
          onClick={() => setTab('triage')}
        >
          Triage
        </button>
        <button
          className={tab === 'queue' ? 'active' : ''}
          onClick={() => {
            setTab('queue')
            void refresh()
          }}
        >
          Review queue
          {queue.length > 0 && (
            <span className="tab-count alert">{queue.length}</span>
          )}
        </button>
        <button
          className={tab === 'metrics' ? 'active' : ''}
          onClick={() => {
            setTab('metrics')
            void refresh()
          }}
        >
          Metrics
          {metrics && metrics.total > 0 && (
            <span className="tab-count">{metrics.total}</span>
          )}
        </button>
      </nav>

      {tab === 'triage' && (
        <div className="columns">
          <div className="panel">
            <h2>Incident report</h2>
            <p className="hint">
              Paste raw, unstructured text — an email, a phone note, a
              monitoring alert. Or start from an example below.
            </p>

            <div className="samples">
              {samples.map((sample) => (
                <button
                  key={sample.id}
                  className={`sample-chip ${
                    activeSample?.id === sample.id ? 'active' : ''
                  }`}
                  onClick={() => pickSample(sample)}
                >
                  {sample.label}
                </button>
              ))}
            </div>

            {activeSample && (
              <p className="sample-note">
                Demonstrates: {activeSample.demonstrates}
              </p>
            )}

            <textarea
              value={text}
              placeholder="The claims portal is down for all branch users since 09:00…"
              onChange={(event) => {
                setText(event.target.value)
                setActiveSample(null)
              }}
              onKeyDown={(event) => {
                // Ctrl/Cmd+Enter submits: this screen gets used repeatedly in
                // a sitting, and reaching for the mouse each time grates.
                if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                  void submit()
                }
              }}
            />

            <div className="row">
              <button className="primary" onClick={submit} disabled={busy || !text.trim()}>
                {busy ? 'Triaging…' : 'Triage incident'}
              </button>
              <button
                className="ghost"
                onClick={() => {
                  setText('')
                  setResult(null)
                  setActiveSample(null)
                  setError(null)
                }}
                disabled={busy}
              >
                Clear
              </button>
              <span className="spacer" />
              <span className="char-count">{text.length} chars · ⌘/Ctrl+↵</span>
            </div>
          </div>

          <div>
            {error && <div className="error-box">{error}</div>}
            {result && config ? (
              <TriageResultView
                result={result}
                threshold={config.confidence_threshold}
              />
            ) : (
              !error && (
                <div className="panel">
                  <div className="empty">
                    The structured result appears here: category, priority,
                    summary, recommended action, per-quote grounding checks and
                    the routing decision.
                  </div>
                </div>
              )
            )}
          </div>
        </div>
      )}

      {tab === 'queue' &&
        (config ? (
          <ReviewQueue queue={queue} config={config} onRefresh={refresh} />
        ) : (
          <div className="panel">
            <div className="empty">Loading…</div>
          </div>
        ))}

      {tab === 'metrics' &&
        (metrics ? (
          <MetricsView metrics={metrics} />
        ) : (
          <div className="panel">
            <div className="empty">Loading…</div>
          </div>
        ))}
    </div>
  )
}
