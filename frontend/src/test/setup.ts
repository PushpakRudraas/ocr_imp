import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// react-router v6 prints v7 migration notices on every MemoryRouter; they are noise in test output.
const warn = console.warn;
vi.spyOn(console, 'warn').mockImplementation((...a) => { if (!String(a[0]).includes('React Router Future Flag')) warn(...a); });
