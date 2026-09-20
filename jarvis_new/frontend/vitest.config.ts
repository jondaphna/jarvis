import path from 'node:path';
import { defineConfig } from 'vitest/config';

/**
 * The dashboard's server-side tests.
 *
 * Only `tests/` is collected, and only in the node environment: what is being
 * tested here is the route handlers - the actual exported `GET`, `POST` and
 * `DELETE` functions Next calls - rather than any component. The re-audit was
 * explicit that a helper's own unit test does not close F01, because the hole
 * was the route attaching the control token, not the helper.
 */
export default defineConfig({
  resolve: {
    alias: { '@': path.resolve(__dirname) },
  },
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
    restoreMocks: true,
  },
});
