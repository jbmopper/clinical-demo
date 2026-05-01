<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getLayerThreeCalibration,
		listEvalRuns,
		saveLayerThreeCalibration,
		type EvalRunRow,
		type JudgeLabel,
		type LayerThreeCalibrationRow,
		type LayerThreeHumanLabel
	} from '$lib/api';

	let runs = $state<EvalRunRow[]>([]);
	let selectedRunId = $state('');
	let limit = $state(25);
	let labelPath = $state('');
	let reviewer = $state('');
	let rows = $state<LayerThreeCalibrationRow[]>([]);
	let labels = $state<Record<string, LayerThreeHumanLabel>>({});
	let loading = $state(false);
	let saving = $state(false);
	let error = $state<string | null>(null);
	let savedMessage = $state<string | null>(null);

	let labeledCount = $derived(rows.filter((row) => labels[rowKey(row)]?.label != null).length);

	onMount(() => {
		void loadRuns();
	});

	async function loadRuns() {
		error = null;
		try {
			runs = await listEvalRuns();
			if (runs.length && !selectedRunId) {
				selectedRunId = runs[0].run_id;
				await loadRows();
			}
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		}
	}

	async function loadRows() {
		if (!selectedRunId) return;
		loading = true;
		error = null;
		savedMessage = null;
		try {
			const response = await getLayerThreeCalibration(selectedRunId, limit);
			rows = response.rows;
			labelPath = response.label_path;
			const nextLabels: Record<string, LayerThreeHumanLabel> = {};
			for (const row of response.rows) {
				if (row.existing_label) {
					nextLabels[rowKey(row)] = row.existing_label;
				}
			}
			labels = nextLabels;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	function rowKey(row: LayerThreeCalibrationRow): string {
		return `${row.pair_id}:${row.criterion_index}`;
	}

	function ensureLabel(row: LayerThreeCalibrationRow): LayerThreeHumanLabel {
		const key = rowKey(row);
		if (!labels[key]) {
			labels[key] = {
				pair_id: row.pair_id,
				criterion_index: row.criterion_index,
				label: null,
				reviewer: reviewer || null,
				rationale: ''
			};
		}
		return labels[key];
	}

	function setLabel(row: LayerThreeCalibrationRow, label: JudgeLabel) {
		const current = ensureLabel(row);
		current.label = label;
		current.reviewer = reviewer || null;
	}

	function setRationale(row: LayerThreeCalibrationRow, rationale: string) {
		const current = ensureLabel(row);
		current.rationale = rationale;
		current.reviewer = reviewer || null;
	}

	async function saveLabels() {
		saving = true;
		error = null;
		savedMessage = null;
		try {
			const payload = Object.values(labels).filter((label) => label.label !== null);
			const response = await saveLayerThreeCalibration(payload, labelPath || undefined);
			labelPath = response.label_path;
			savedMessage = `Saved ${response.saved} labels to ${response.label_path}`;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			saving = false;
		}
	}

	function formatRun(run: EvalRunRow): string {
		const notes = run.notes ? ` · ${run.notes}` : '';
		return `${run.run_id} · ${run.n_cases} cases${notes}`;
	}
</script>

<section class="calibration">
	<header class="top">
		<div>
			<h2>Layer-3 Calibration</h2>
			<p>
				Label matcher verdicts as human ground truth for
				<code>scripts/eval.py judge --human-labels</code>.
			</p>
		</div>
		<div class="progress">
			<strong>{labeledCount}</strong>/<span>{rows.length}</span>
			<span class="progress-label">labeled</span>
		</div>
	</header>

	<div class="controls">
		<label>
			<span>Run</span>
			<select bind:value={selectedRunId} disabled={!runs.length || loading} onchange={loadRows}>
				{#each runs as run (run.run_id)}
					<option value={run.run_id}>{formatRun(run)}</option>
				{/each}
			</select>
		</label>
		<label class="small">
			<span>Sample limit</span>
			<input type="number" min="1" max="250" bind:value={limit} disabled={loading} />
		</label>
		<label class="small">
			<span>Reviewer</span>
			<input type="text" bind:value={reviewer} placeholder="optional" />
		</label>
		<button onclick={loadRows} disabled={!selectedRunId || loading}>
			{#if loading}loading…{:else}load{/if}
		</button>
		<button class="save" onclick={saveLabels} disabled={!rows.length || saving}>
			{#if saving}saving…{:else}save labels{/if}
		</button>
	</div>

	{#if labelPath}
		<p class="path">Label file: <code>{labelPath}</code></p>
	{/if}

	{#if error}
		<div class="banner err">Calibration failed: <code>{error}</code></div>
	{/if}
	{#if savedMessage}
		<div class="banner ok">{savedMessage}</div>
	{/if}

	{#if !runs.length && !error}
		<p class="empty">No persisted eval runs found. Run <code>scripts/eval.py run</code> first.</p>
	{:else if rows.length === 0 && !loading}
		<p class="empty">Choose an eval run and load a calibration sample.</p>
	{:else}
		<div class="cards">
			{#each rows as row (rowKey(row))}
				{@const current = labels[rowKey(row)]}
				<article class="card">
					<header>
						<div>
							<span class="bucket">{row.bucket}</span>
							<strong>{row.pair_id}</strong>
							<span class="muted">criterion #{row.criterion_index}</span>
						</div>
						<div class="tags">
							<span>{row.criterion_kind}</span>
							<span>{row.polarity}</span>
							<span>{row.negated ? 'negated' : 'not negated'}</span>
							<span>{row.mood}</span>
						</div>
					</header>

					<div class="grid">
						<section>
							<h3>Criterion</h3>
							<p class="criterion">{row.criterion_source_text}</p>
						</section>
						<section>
							<h3>Matcher Verdict</h3>
							<p>
								<strong>{row.matcher_verdict}</strong>
								<span class="muted"> · {row.matcher_reason}</span>
							</p>
							<p>{row.matcher_rationale}</p>
						</section>
					</div>

					{#if row.evidence.length}
						<details>
							<summary>Evidence JSON ({row.evidence.length})</summary>
							<pre>{JSON.stringify(row.evidence, null, 2)}</pre>
						</details>
					{:else}
						<p class="muted">No cited evidence.</p>
					{/if}

					<div class="labels">
						<label>
							<input
								type="radio"
								name={rowKey(row)}
								checked={current?.label === 'correct'}
								onchange={() => setLabel(row, 'correct')}
							/>
							correct
						</label>
						<label>
							<input
								type="radio"
								name={rowKey(row)}
								checked={current?.label === 'incorrect'}
								onchange={() => setLabel(row, 'incorrect')}
							/>
							incorrect
						</label>
						<label>
							<input
								type="radio"
								name={rowKey(row)}
								checked={current?.label === 'unjudgeable'}
								onchange={() => setLabel(row, 'unjudgeable')}
							/>
							unjudgeable
						</label>
					</div>

					<label class="rationale">
						<span>Reviewer rationale</span>
						<textarea
							value={current?.rationale ?? ''}
							oninput={(event) =>
								setRationale(row, (event.currentTarget as HTMLTextAreaElement).value)}
							placeholder="optional note for calibration review"
						></textarea>
					</label>
				</article>
			{/each}
		</div>
	{/if}
</section>

<style>
	.calibration {
		background: white;
		border: 1px solid #e2e8f0;
		border-radius: 10px;
		padding: 18px;
	}
	.top {
		display: flex;
		justify-content: space-between;
		gap: 16px;
		margin-bottom: 16px;
	}
	h2,
	h3,
	p {
		margin-top: 0;
	}
	h2 {
		margin-bottom: 4px;
		font-size: 1.1rem;
	}
	h3 {
		margin-bottom: 6px;
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #64748b;
	}
	.progress {
		min-width: 96px;
		padding: 10px;
		text-align: center;
		background: #f8fafc;
		border: 1px solid #e2e8f0;
		border-radius: 10px;
	}
	.progress strong {
		font-size: 1.35rem;
	}
	.progress-label {
		display: block;
		color: #64748b;
		font-size: 0.75rem;
	}
	.controls {
		display: grid;
		grid-template-columns: minmax(260px, 2fr) 120px 160px auto auto;
		gap: 12px;
		align-items: end;
		margin-bottom: 12px;
	}
	.controls label,
	.rationale {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.85rem;
	}
	.controls span,
	.rationale span {
		color: #475569;
		font-weight: 600;
	}
	select,
	input,
	textarea {
		padding: 6px 10px;
		border: 1px solid #cbd5e1;
		border-radius: 6px;
		background: white;
		min-width: 0;
		font: inherit;
	}
	textarea {
		min-height: 72px;
		resize: vertical;
	}
	button {
		padding: 8px 12px;
		border: 1px solid #cbd5e1;
		border-radius: 8px;
		background: white;
		font-weight: 600;
	}
	button.save {
		background: #0f172a;
		color: white;
		border-color: #0f172a;
	}
	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.path,
	.empty,
	.muted {
		color: #64748b;
		font-size: 0.85rem;
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
	.banner.ok {
		background: #dcfce7;
		color: #14532d;
		border: 1px solid #86efac;
	}
	.cards {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.card {
		border: 1px solid #e2e8f0;
		border-radius: 10px;
		padding: 14px;
	}
	.card header,
	.tags,
	.labels {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
		align-items: center;
	}
	.card header {
		justify-content: space-between;
		margin-bottom: 12px;
	}
	.bucket,
	.tags span {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 999px;
		background: #f1f5f9;
		color: #334155;
		font-size: 0.75rem;
	}
	.bucket {
		background: #dbeafe;
		color: #1e3a8a;
	}
	.grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 16px;
	}
	.criterion {
		white-space: pre-wrap;
	}
	details {
		margin: 10px 0;
	}
	pre {
		overflow-x: auto;
		padding: 10px;
		border-radius: 8px;
		background: #0f172a;
		color: #e2e8f0;
		font-size: 0.78rem;
	}
	.labels {
		margin: 12px 0;
		padding-top: 12px;
		border-top: 1px solid #e2e8f0;
	}
	.labels label {
		display: flex;
		align-items: center;
		gap: 4px;
		font-weight: 600;
	}
	@media (max-width: 900px) {
		.controls,
		.grid {
			grid-template-columns: 1fr;
		}
	}
</style>
