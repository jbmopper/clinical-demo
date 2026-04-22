<script lang="ts">
	import type { MatchVerdict } from './api';
	import VerdictPill from './VerdictPill.svelte';

	let { v }: { v: MatchVerdict } = $props();
	let open = $state(false);

	function shortKind(): string {
		const k = v.criterion.kind;
		const pol = v.criterion.polarity === 'inclusion' ? 'INCL' : 'EXCL';
		return `${pol} · ${k}`;
	}
</script>

<div class="row" class:open>
	<button class="head" onclick={() => (open = !open)} aria-expanded={open}>
		<span class="kind">{shortKind()}</span>
		<span class="text">{v.criterion.source_text}</span>
		<VerdictPill verdict={v.verdict} reason={v.reason} />
		<span class="chev" aria-hidden="true">{open ? '▾' : '▸'}</span>
	</button>

	{#if open}
		<div class="body">
			<div class="rationale">{v.rationale}</div>

			{#if v.evidence.length === 0}
				<div class="muted">No evidence cited.</div>
			{:else}
				<ul class="evidence">
					{#each v.evidence as e (e.kind + e.note)}
						<li>
							<span class="ekind">{e.kind}</span>
							<span class="enote">{e.note}</span>
						</li>
					{/each}
				</ul>
			{/if}

			<details class="raw">
				<summary>raw criterion</summary>
				<pre>{JSON.stringify(v.criterion, null, 2)}</pre>
			</details>
		</div>
	{/if}
</div>

<style>
	.row {
		border: 1px solid #e2e8f0;
		border-radius: 8px;
		background: white;
		margin-bottom: 8px;
		overflow: hidden;
	}
	.row.open {
		border-color: #94a3b8;
	}
	.head {
		all: unset;
		display: grid;
		grid-template-columns: 100px 1fr auto 16px;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 10px 14px;
		cursor: pointer;
	}
	.head:hover {
		background: #f1f5f9;
	}
	.kind {
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		color: #475569;
	}
	.text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.92rem;
	}
	.chev {
		color: #94a3b8;
		font-size: 0.85rem;
	}
	.body {
		padding: 8px 14px 14px 14px;
		border-top: 1px dashed #e2e8f0;
		background: #fafbfc;
	}
	.rationale {
		font-size: 0.92rem;
		margin-bottom: 8px;
	}
	.muted {
		color: #94a3b8;
		font-size: 0.85rem;
		font-style: italic;
	}
	.evidence {
		list-style: none;
		padding: 0;
		margin: 0 0 8px 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.evidence li {
		display: grid;
		grid-template-columns: 100px 1fr;
		gap: 12px;
		font-size: 0.85rem;
		padding: 4px 8px;
		background: white;
		border: 1px solid #e2e8f0;
		border-radius: 6px;
	}
	.ekind {
		font-weight: 600;
		color: #475569;
		text-transform: uppercase;
		font-size: 0.7rem;
		letter-spacing: 0.04em;
		align-self: center;
	}
	.enote {
		min-width: 0;
		word-break: break-word;
	}
	.raw {
		margin-top: 8px;
	}
	.raw summary {
		font-size: 0.78rem;
		color: #64748b;
		cursor: pointer;
	}
	.raw pre {
		margin: 6px 0 0 0;
		padding: 8px;
		background: #0f172a;
		color: #e2e8f0;
		border-radius: 6px;
		max-height: 280px;
		overflow: auto;
	}
</style>
