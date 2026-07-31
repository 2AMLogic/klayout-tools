/**
 * Vitest global setup — registers `@testing-library/jest-dom`'s DOM
 * matchers (`toBeInTheDocument`, `toBeChecked`, ...) for every test file.
 * Safe to load even for the suite's non-DOM (node-environment) tests: it
 * only extends `expect`, it doesn't touch `window`/`document`.
 */
import "@testing-library/jest-dom/vitest";
