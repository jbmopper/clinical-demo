<script lang="ts">
	import type { Verdict } from './api';
	let { verdict, reason }: { verdict: Verdict; reason?: string } = $props();
	const COLORS: Record<Verdict, { bg: string; fg: string; border: string }> = {
		pass: { bg: '#dcfce7', fg: '#14532d', border: '#86efac' },
		fail: { bg: '#fee2e2', fg: '#7f1d1d', border: '#fca5a5' },
		indeterminate: { bg: '#fef3c7', fg: '#78350f', border: '#fcd34d' }
	};
	let c = $derived(COLORS[verdict]);
</script>

<span
	class="pill"
	style:background={c.bg}
	style:color={c.fg}
	style:border-color={c.border}
	title={reason ? `${verdict} — ${reason}` : verdict}
>
	{verdict}
	{#if reason && reason !== 'ok'}
		<span class="reason">· {reason}</span>
	{/if}
</span>

<style>
	.pill {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 2px 10px;
		border-radius: 999px;
		border: 1px solid;
		font-size: 0.78rem;
		font-weight: 600;
		letter-spacing: 0.02em;
		text-transform: uppercase;
		white-space: nowrap;
	}
	.reason {
		font-weight: 500;
		text-transform: none;
		letter-spacing: 0;
		opacity: 0.85;
	}
</style>
