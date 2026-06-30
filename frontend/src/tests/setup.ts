import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// Unmount any rendered React trees and reset the jsdom DOM after every test so
// suites stay isolated. (Redundant with RTL auto-cleanup under globals: true,
// but explicit and harmless — keeps isolation deterministic.)
afterEach(() => {
  cleanup();
});
