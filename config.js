/**
 * Public runtime configuration.
 *
 * Everything here is readable by anyone who opens the site. Never add API keys,
 * tokens or passwords. See docs/environment.md for what stays on the backend.
 *
 * Values come from js/env.js (generated from .env by `npm run env`).
 * Defaults keep the app working in mock mode if env.js is missing.
 */
const raw = (typeof window !== 'undefined' && window.__ENV__) || {};

const bool = (v, fallback) => (v === undefined || v === '' ? fallback : String(v).toLowerCase() === 'true');
const num = (v, fallback) => (v === undefined || v === '' || !Number.isFinite(Number(v)) ? fallback : Number(v));

export const config = Object.freeze({
  appEnv: raw.APP_ENV || 'development',
  apiBaseUrl: String(raw.API_BASE_URL || 'http://localhost:8000/api/v1').replace(/\/+$/, ''),
  apiTimeoutMs: num(raw.API_TIMEOUT_MS, 15000),
  useMocks: bool(raw.USE_MOCKS, true),
  authMode: raw.AUTH_MODE === 'bearer' ? 'bearer' : 'cookie',
  defaults: Object.freeze({
    vlm: raw.DEFAULT_VLM || 'OmniVision-VL 7B',
    llm: raw.DEFAULT_LLM || 'OmniCoder-LLM 70B'
  }),
  features: Object.freeze({
    deepScan: bool(raw.ENABLE_DEEP_SCAN, true),
    prCreation: bool(raw.ENABLE_PR_CREATION, false)
  })
});

export const isProduction = config.appEnv === 'production';

if (isProduction && config.useMocks) console.warn('[config] Production build is running with mock data (PUBLIC_USE_MOCKS=true).');
if (isProduction && !config.apiBaseUrl.startsWith('https://')) console.warn('[config] Production API URL should use https.');
