import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from './api'

// The API layer exists to make failures legible. These tests cover the cases
// that otherwise produce a silently blank panel: a backend that is not running,
// and a 422 whose useful detail is buried in a nested FastAPI error array.

describe('api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('returns the parsed body on success', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: 'OK', incident_id: 'INC-1' }),
    } as Response)

    await expect(api.triage('The portal is down')).resolves.toMatchObject({
      incident_id: 'INC-1',
    })
  })

  it('explains a dead backend in terms the reader can act on', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('Failed to fetch'))

    await expect(api.metrics()).rejects.toThrow(/Is the backend running/)
    await expect(api.metrics()).rejects.toBeInstanceOf(ApiError)
  })

  it('unpacks a FastAPI validation error into a readable message', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({
        detail: [{ loc: ['body', 'text'], msg: 'String should have at least 1 character' }],
      }),
    } as Response)

    await expect(api.triage('')).rejects.toThrow(/text: String should have at least 1 character/)
  })

  it('passes through a plain string detail', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'Incident is not in the review queue.' }),
    } as Response)

    await expect(
      api.submitReview({ incident_id: 'INC-NOPE', accepted: true }),
    ).rejects.toThrow(/not in the review queue/)
  })

  it('falls back to the status code when the body is not JSON', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error('not json')
      },
    } as unknown as Response)

    await expect(api.metrics()).rejects.toThrow(/status 500/)
  })

  it('sends the incident as a POST body', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
    } as Response)

    await api.triage('VPN is down for the Durban office')

    const [path, init] = vi.mocked(fetch).mock.calls[0]
    expect(path).toBe('/api/triage')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      text: 'VPN is down for the Durban office',
    })
  })
})
