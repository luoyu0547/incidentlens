/**
 * MSW test server for the API v1 read surface.
 *
 * Starts before all tests and resets between tests so handler state is
 * isolated. The server is torn down after all tests complete.
 */
import { setupServer } from 'msw/node';
import { handlers } from './handlers';

export const server = setupServer(...handlers);
