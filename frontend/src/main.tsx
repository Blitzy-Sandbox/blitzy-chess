/**
 * main.tsx — Vite/React entry point for the Blitzy Chess single-page application.
 *
 * The single bootstrap module for the SPA. It mounts the React tree into the
 * `#root` element declared in `index.html` and imports the global stylesheet for
 * its side effects (the Tailwind base/components/utilities layers plus the
 * board/dark-panel theme tokens). It deliberately holds NO application logic —
 * all screens, routing, and state live in {@link App} and its children
 * (AAP §0.5.1 Group 8).
 *
 * Implementation notes:
 *   - React 18 client API: `ReactDOM.createRoot(...).render(...)`, not the legacy
 *     React 17 `ReactDOM.render`.
 *   - JSX uses the automatic runtime (`tsconfig.json` `jsx: "react-jsx"`), so no
 *     default `React` import is required; only the named `StrictMode` is imported.
 *   - `StrictMode` is kept (the Vite + React default). In development it
 *     intentionally double-invokes effects to surface unsafe side effects, which
 *     is why the WebSocket hooks implement clean teardown on unmount.
 *   - `#root` is guaranteed by `index.html`, so the lookup uses a non-null
 *     assertion rather than a runtime guard.
 *
 * @module main
 */
import { StrictMode } from 'react';
import ReactDOM from 'react-dom/client';

import App from './App';
import './styles/index.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
