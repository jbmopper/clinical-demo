import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	kit: {
		adapter: adapter({
			fallback: 'index.html'
		}),
		// Local dev rig only — any unhandled prerender/runtime
		// errors fall back to a plain index.html.
		prerender: {
			handleHttpError: 'warn'
		}
	}
};

export default config;
