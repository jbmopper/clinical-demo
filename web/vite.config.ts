import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		// Keep API calls explicit (no proxy). The dev UI hits
		// http://127.0.0.1:8000 directly — CORS on the FastAPI side
		// is wide-open for the v0 demo. When this UI moves into the
		// juliusm.com repo, swap the API base URL via VITE_API_BASE.
		port: 5173
	}
});
