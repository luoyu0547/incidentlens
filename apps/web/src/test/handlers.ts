/**
 * MSW request handlers for the API v1 read surface.
 *
 * Every handler returns valid JSON matching the generated protocol types.
 * Unknown paths or unmatched handlers let the request fall through (no
 * passthrough — the test will fail with a MSW warning instead of a silent
 * network miss).
 */
import { http, HttpResponse } from 'msw';
import {
  overviewFixture,
  targetServicesFixture,
  serviceFixture,
  logPageFixture,
  issuePageFixture,
  issueFixture,
  investigationPageFixture,
  investigationFixture,
  evidenceFixture,
} from './fixtures';

const API_ROOT = '/api/v1';

export const handlers = [
  http.get('/events/v1/workspace', () => new HttpResponse(null, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })),
  http.get(`${API_ROOT}/overview`, () => HttpResponse.json(overviewFixture)),

  http.get(`${API_ROOT}/targets/:targetId/services`, () => HttpResponse.json(targetServicesFixture)),

  http.get(`${API_ROOT}/services/:serviceId`, () => HttpResponse.json(serviceFixture)),

  http.get(`${API_ROOT}/services/:serviceId/logs`, ({ request }) => {
    void request;
    return HttpResponse.json(logPageFixture);
  }),

  http.get(`${API_ROOT}/issues`, () => HttpResponse.json(issuePageFixture)),

  http.get(`${API_ROOT}/issues/:issueId`, () => HttpResponse.json(issueFixture)),

  http.get(`${API_ROOT}/investigations`, () => HttpResponse.json(investigationPageFixture)),

  http.get('http://localhost:3000/api/v1/investigations/:investigationId/summary', () => HttpResponse.json(investigationFixture)),

  http.get(`${API_ROOT}/evidence/:evidenceRefId`, () => HttpResponse.json(evidenceFixture)),
];
