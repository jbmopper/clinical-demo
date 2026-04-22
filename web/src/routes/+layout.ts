// Static-only adapter: no server-side rendering, no prerender.
// This dev rig is purely a SPA against the local FastAPI.
export const ssr = false;
export const prerender = false;
export const trailingSlash = 'never';
