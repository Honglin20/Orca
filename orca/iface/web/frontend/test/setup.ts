import "@testing-library/jest-dom/vitest";

// vitest global setup. happy-dom provides window/document. fetch and WebSocket
// are mocked per-test (vi.stubGlobal / vi.spyOn) so tests stay hermetic.
