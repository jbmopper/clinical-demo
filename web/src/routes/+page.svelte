<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getHealth,
		listPatients,
		listTrials,
		score,
		type PatientRow,
		type ScorePairResult,
		type TrialRow
	} from '$lib/api';
	import CriterionRow from '$lib/CriterionRow.svelte';
	import VerdictPill from '$lib/VerdictPill.svelte';

	// catalog state
	let patients = $state<PatientRow[]>([]);
	let trials = $state<TrialRow[]>([]);
	let catalogError = $state<string | null>(null);
	let healthOk = $state<boolean | null>(null);

	// form state
	let patientId = $state<string>('');
	let nctId = $state<string>('');
	let asOf = $state<string>(new Date().toISOString().slice(0, 10));
	let orchestrator = $state<'imperative' | 'graph'>('imperative');
	let criticEnabled = $state<boolean>(false);
	let useCachedExtraction = $state<boolean>(true);

	// run state
	let running = $state<boolean>(false);
	let result = $state<ScorePairResult | null>(null);
	let runError = $state<string | null>(null);

	let canRun = $derived(!!patientId && !!nctId && !!asOf && !running);

	onMount(async () => {
		try {
			const h = await getHealth();
			healthOk = h.status === 'ok';
		} catch {
			healthOk = false;
		}
		try {
			const [ps, ts] = await Promise.all([listPatients(), listTrials()]);
			patients = ps;
			trials = ts;
			if (ps.length && !patientId) patientId = ps[0].patient_id;
			if (ts.length && !nctId) nctId = ts[0].nct_id;
		} catch (err) {
			catalogError = err instanceof Error ? err.message : String(err);
		}
	});

	async function runScore() {
		if (!canRun) return;
		running = true;
		runError = null;
		result = null;
		try {
			result = await score({
				patient_id: patientId,
				nct_id: nctId,
				as_of: asOf,
				orchestrator,
				critic_enabled: criticEnabled,
				use_cached_extraction: useCachedExtraction
			});
		} catch (err) {
			runError = err instanceof Error ? err.message : String(err);
		} finally {
			running = false;
		}
	}

	function fmtCost(usd: number | null | undefined): string {
		if (usd === null || usd === undefined) return '—';
		return `$${usd.toFixed(4)}`;
	}
	function fmtTokens(t: number | null | undefined): string {
		if (t === null || t === undefined) return '—';
		return t.toLocaleString();
	}
</script>

<header class="hdr">
	<div>
		<h1>clinical-demo · reviewer</h1>
		<p class="sub">
			Local dev rig over <code>POST /score</code>. v0 — see PLAN tasks 2.8 / 2.9.
		</p>
	</div>
	<div class="health" class:ok={healthOk === true} class:bad={healthOk === false}>
		api:
		{#if healthOk === null}
			<em>checking…</em>
		{:else if healthOk}
			ok
		{:else}
			unreachable
		{/if}
	</div>
</header>

{#if catalogError}
	<div class="banner err">
		Couldn't load catalog from API: <code>{catalogError}</code>. Is
		<code>scripts/serve.py</code> running?
	</div>
{/if}

<section class="form">
	<label>
		<span>Patient</span>
		<select bind:value={patientId} disabled={!patients.length}>
			{#each patients as p (p.patient_id)}
				<option value={p.patient_id}>
					{p.patient_id}{p.slice ? ` · ${p.slice}` : ''}
				</option>
			{/each}
		</select>
	</label>

	<label>
		<span>Trial</span>
		<select bind:value={nctId} disabled={!trials.length}>
			{#each trials as t (t.nct_id)}
				<option value={t.nct_id} title={t.title}>
					{t.nct_id} — {t.title}
				</option>
			{/each}
		</select>
	</label>

	<label class="small">
		<span>As of</span>
		<input type="date" bind:value={asOf} />
	</label>

	<label class="small">
		<span>Orchestrator</span>
		<select bind:value={orchestrator}>
			<option value="imperative">imperative</option>
			<option value="graph">graph (LangGraph)</option>
		</select>
	</label>

	<label class="check">
		<input
			type="checkbox"
			bind:checked={criticEnabled}
			disabled={orchestrator !== 'graph'}
		/>
		<span>critic loop (graph only)</span>
	</label>

	<label class="check">
		<input type="checkbox" bind:checked={useCachedExtraction} />
		<span>use cached extraction</span>
	</label>

	<button class="run" onclick={runScore} disabled={!canRun}>
		{#if running}scoring…{:else}score{/if}
	</button>
</section>

{#if runError}
	<div class="banner err">
		Score failed: <code>{runError}</code>
	</div>
{/if}

{#if result}
	<section class="result">
		<header class="rhdr">
			<div class="rmeta">
				<div>
					<span class="lbl">eligibility</span>
					<VerdictPill verdict={result.eligibility} />
				</div>
				<div>
					<span class="lbl">criteria</span>
					<strong>{result.summary.total_criteria}</strong>
					<span class="muted">
						(pass {result.summary.by_verdict.pass ?? 0} ·
						fail {result.summary.by_verdict.fail ?? 0} ·
						indet {result.summary.by_verdict.indeterminate ?? 0})
					</span>
				</div>
				<div>
					<span class="lbl">extractor</span>
					<code>{result.extraction_meta.model}</code>
					<span class="muted">· {result.extraction_meta.prompt_version}</span>
				</div>
				<div>
					<span class="lbl">cost / tokens</span>
					{fmtCost(result.extraction_meta.cost_usd)}
					<span class="muted">
						· in {fmtTokens(result.extraction_meta.input_tokens)}
						· out {fmtTokens(result.extraction_meta.output_tokens)}
					</span>
				</div>
			</div>
		</header>

		<h2>Per-criterion verdicts</h2>
		{#if result.verdicts.length === 0}
			<p class="muted">No criteria extracted.</p>
		{:else}
			<div class="verdicts">
				{#each result.verdicts as v, i (i)}
					<CriterionRow {v} />
				{/each}
			</div>
		{/if}
	</section>
{/if}

<style>
	.hdr {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: 18px;
	}
	.hdr h1 {
		margin: 0 0 4px 0;
		font-size: 1.35rem;
	}
	.sub {
		margin: 0;
		color: #64748b;
		font-size: 0.9rem;
	}
	.health {
		font-size: 0.78rem;
		font-weight: 600;
		padding: 4px 10px;
		border-radius: 999px;
		background: #f1f5f9;
		color: #475569;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.health.ok {
		background: #dcfce7;
		color: #14532d;
	}
	.health.bad {
		background: #fee2e2;
		color: #7f1d1d;
	}

	.banner {
		padding: 10px 14px;
		border-radius: 8px;
		margin-bottom: 14px;
		font-size: 0.9rem;
	}
	.banner.err {
		background: #fee2e2;
		color: #7f1d1d;
		border: 1px solid #fca5a5;
	}

	.form {
		display: grid;
		grid-template-columns: 2fr 2fr 1fr 1fr;
		gap: 12px;
		align-items: end;
		padding: 16px;
		background: white;
		border: 1px solid #e2e8f0;
		border-radius: 10px;
		margin-bottom: 18px;
	}
	.form label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.85rem;
	}
	.form label span {
		color: #475569;
		font-weight: 600;
	}
	.form select,
	.form input[type='date'] {
		padding: 6px 10px;
		border: 1px solid #cbd5e1;
		border-radius: 6px;
		background: white;
		min-width: 0;
	}
	.form .check {
		flex-direction: row;
		align-items: center;
		gap: 6px;
	}
	.run {
		grid-column: 4;
		grid-row: 3;
		padding: 10px 16px;
		background: #0f172a;
		color: white;
		border: none;
		border-radius: 8px;
		font-weight: 600;
	}
	.run:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.result {
		background: white;
		border: 1px solid #e2e8f0;
		border-radius: 10px;
		padding: 18px;
	}
	.rhdr {
		margin-bottom: 18px;
		padding-bottom: 14px;
		border-bottom: 1px solid #e2e8f0;
	}
	.rmeta {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
		gap: 12px;
		font-size: 0.9rem;
	}
	.lbl {
		display: block;
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #64748b;
		font-weight: 600;
		margin-bottom: 4px;
	}
	.muted {
		color: #94a3b8;
		font-size: 0.85rem;
	}
	.result h2 {
		font-size: 1rem;
		margin: 0 0 10px 0;
		color: #334155;
	}
</style>
