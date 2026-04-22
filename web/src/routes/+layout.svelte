<script lang="ts">
	let { children } = $props();
</script>

<div class="app">
	{@render children()}
</div>

<style>
	:global(html, body) {
		margin: 0;
		padding: 0;
		background: #f8fafc;
		color: #0f172a;
		font-family:
			ui-sans-serif,
			system-ui,
			-apple-system,
			'Segoe UI',
			Roboto,
			'Helvetica Neue',
			Arial,
			sans-serif;
		line-height: 1.45;
	}
	:global(*, *::before, *::after) {
		box-sizing: border-box;
	}
	:global(button) {
		font: inherit;
		cursor: pointer;
	}
	:global(code, pre) {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85em;
	}
	.app {
		min-height: 100vh;
		max-width: 1200px;
		margin: 0 auto;
		padding: 24px;
	}
</style>
