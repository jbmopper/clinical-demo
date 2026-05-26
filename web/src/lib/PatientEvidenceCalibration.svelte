<script lang="ts">
	import { onMount } from 'svelte';
	import {
		getPatientEvidenceCalibration,
		savePatientEvidenceCalibration,
		type PatientEvidenceCalibrationRow,
		type PatientEvidenceHumanLabel,
		type PatientEvidenceLabel,
		type MatcherAssumptionMode,
		type PatientEvidenceSourceRow,
		type PatientEvidenceCompositeSubcheck
	} from '$lib/api';

	let candidatePath = $state('');
	let labelPath = $state('');
	let reviewer = $state('');
	let sourceSearch = $state('');
	let rows = $state<PatientEvidenceCalibrationRow[]>([]);
	let labels = $state<Record<string, PatientEvidenceHumanLabel>>({});
	let loading = $state(false);
	let saving = $state(false);
	let error = $state<string | null>(null);
	let savedMessage = $state<string | null>(null);

	let labeledCount = $derived(rows.filter((row) => labels[rowKey(row)]?.label != null).length);

	onMount(() => {
		void loadRows();
	});

	async function loadRows() {
		loading = true;
		error = null;
		savedMessage = null;
		try {
			const response = await getPatientEvidenceCalibration();
			candidatePath = response.candidate_path;
			labelPath = response.label_path;
			rows = response.rows;
			const nextLabels: Record<string, PatientEvidenceHumanLabel> = {};
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

	function rowKey(row: PatientEvidenceCalibrationRow): string {
		return `${row.pair_id}:${row.criterion_index}`;
	}

	function ensureLabel(row: PatientEvidenceCalibrationRow): PatientEvidenceHumanLabel {
		const key = rowKey(row);
		if (!labels[key]) {
			labels[key] = {
				pair_id: row.pair_id,
				criterion_index: row.criterion_index,
				label: null,
				cited_source_row_ids: [],
				expected_matcher_verdict: null,
				matcher_assumption_mode: row.matcher_assumption_mode,
				reviewer: reviewer || null,
				rationale: ''
			};
		}
		return labels[key];
	}

	function setEvidenceLabel(row: PatientEvidenceCalibrationRow, label: PatientEvidenceLabel) {
		const current = ensureLabel(row);
		current.label = label;
		current.reviewer = reviewer || null;
		labels = { ...labels, [rowKey(row)]: current };
	}

	function setExpectedVerdict(
		row: PatientEvidenceCalibrationRow,
		verdict: PatientEvidenceHumanLabel['expected_matcher_verdict']
	) {
		const current = ensureLabel(row);
		current.expected_matcher_verdict = verdict;
		current.reviewer = reviewer || null;
		labels = { ...labels, [rowKey(row)]: current };
	}

	function setAssumptionMode(row: PatientEvidenceCalibrationRow, mode: MatcherAssumptionMode) {
		const current = ensureLabel(row);
		current.matcher_assumption_mode = mode;
		current.reviewer = reviewer || null;
		labels = { ...labels, [rowKey(row)]: current };
	}

	function setRationale(row: PatientEvidenceCalibrationRow, rationale: string) {
		const current = ensureLabel(row);
		current.rationale = rationale;
		current.reviewer = reviewer || null;
		labels = { ...labels, [rowKey(row)]: current };
	}

	function toggleCitation(row: PatientEvidenceCalibrationRow, rowId: string) {
		const current = ensureLabel(row);
		const cited = new Set(current.cited_source_row_ids);
		if (cited.has(rowId)) {
			cited.delete(rowId);
		} else {
			cited.add(rowId);
		}
		current.cited_source_row_ids = Array.from(cited).sort();
		current.reviewer = reviewer || null;
		labels = { ...labels, [rowKey(row)]: current };
	}

	async function saveLabels() {
		saving = true;
		error = null;
		savedMessage = null;
		try {
			const payload = Object.values(labels).filter(
				(label) =>
					label.label !== null ||
					label.cited_source_row_ids.length > 0 ||
					label.expected_matcher_verdict !== null ||
					label.matcher_assumption_mode !== 'open_world' ||
					label.rationale.trim().length > 0
			);
			const response = await savePatientEvidenceCalibration(payload, labelPath || undefined);
			labelPath = response.label_path;
			savedMessage = `Saved ${response.saved} labels to ${response.label_path}`;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			saving = false;
		}
	}

	function sourceRows(row: PatientEvidenceCalibrationRow, source: 'patient' | 'trial') {
		const query = sourceSearch.trim().toLowerCase();
		return row.source_rows.filter((sourceRow) => {
			if (sourceRow.source !== source) return false;
			if (!query) return true;
			return sourceSearchText(sourceRow).includes(query);
		});
	}

	function sourceRowCount(row: PatientEvidenceCalibrationRow, source: 'patient' | 'trial') {
		return row.source_rows.filter((sourceRow) => sourceRow.source === source).length;
	}

	function sourceSearchText(sourceRow: PatientEvidenceSourceRow): string {
		return [
			sourceRow.row_id,
			sourceRow.source,
			sourceRow.kind,
			sourceRow.label,
			sourceRow.value,
			sourceRow.date,
			sourceRow.code,
			sourceRow.system,
			sourceRow.status
		]
			.filter(Boolean)
			.join(' ')
			.toLowerCase();
	}

	function rowSummary(sourceRow: PatientEvidenceSourceRow): string {
		const parts = [sourceRow.date, sourceRow.code, sourceRow.status].filter(Boolean);
		return parts.join(' · ');
	}

	function isRetrieved(row: PatientEvidenceCalibrationRow, sourceRow: PatientEvidenceSourceRow) {
		return row.retrieved_source_row_ids.includes(sourceRow.row_id);
	}

	function retrievalReasons(row: PatientEvidenceCalibrationRow, sourceRow: PatientEvidenceSourceRow) {
		return row.retrieval_reasons[sourceRow.row_id] ?? [];
	}

	function sourceRowById(
		row: PatientEvidenceCalibrationRow,
		rowId: string
	): PatientEvidenceSourceRow | undefined {
		return row.source_rows.find((sourceRow) => sourceRow.row_id === rowId);
	}

	function subcheckRows(
		row: PatientEvidenceCalibrationRow,
		subcheck: PatientEvidenceCompositeSubcheck
	): PatientEvidenceSourceRow[] {
		return subcheck.retrieved_source_row_ids
			.map((rowId) => sourceRowById(row, rowId))
			.filter((sourceRow): sourceRow is PatientEvidenceSourceRow => sourceRow !== undefined);
	}

	function assumptionGuidance(
		row: PatientEvidenceCalibrationRow,
		current: PatientEvidenceHumanLabel | undefined
	): string | null {
		const mode = current?.matcher_assumption_mode ?? row.matcher_assumption_mode;
		if (mode === 'open_world') {
			return row.matcher_reason === 'no_data'
				? 'Open world: a missing patient row is insufficient evidence, not proof of absence.'
				: null;
		}
		if (row.matcher_reason === 'unmapped_concept') {
			return 'Closed world cannot repair an unmapped trial concept; keep terminology gaps separate from absence assumptions.';
		}
		if (row.criterion_kind === 'measurement_threshold') {
			return 'Closed-world modes do not turn missing or low-confidence lab measurements into clinical pass/fail decisions.';
		}
		if (row.criterion_kind === 'free_text' || row.matcher_reason === 'human_review_required') {
			return 'Closed world is not a substitute for reviewing free-text criteria against cited patient evidence.';
		}
		return (
			row.closed_world_label_guidance ||
			'Closed world is a synthetic-eval assumption. Use only when this packet is complete for the relevant data type.'
		);
	}

	function reviewHint(row: PatientEvidenceCalibrationRow): string | null {
		if (row.free_text_review_hint === 'interview_required') {
			return 'Interview required: this criterion asks for patient/site information that structured chart evidence usually cannot prove.';
		}
		return null;
	}
</script>

<section class="patient-evidence">
	<header class="top">
		<div>
			<h2>Patient Evidence Calibration</h2>
			<p>
				Label whether patient/trial source rows support the criterion, then save into
				<code>patient_evidence_labels.json</code>.
			</p>
		</div>
		<div class="progress">
			<strong>{labeledCount}</strong>/<span>{rows.length}</span>
			<span class="progress-label">labeled</span>
		</div>
	</header>

	<div class="controls">
		<label>
			<span>Reviewer</span>
			<input type="text" bind:value={reviewer} placeholder="optional" />
		</label>
		<label class="search">
			<span>Search source rows</span>
			<input type="search" bind:value={sourceSearch} placeholder="HbA1c, CKD, metformin…" />
		</label>
		<button onclick={loadRows} disabled={loading}>
			{#if loading}loading…{:else}reload{/if}
		</button>
		<button class="save" onclick={saveLabels} disabled={!rows.length || saving}>
			{#if saving}saving…{:else}save labels{/if}
		</button>
	</div>

	{#if candidatePath || labelPath}
		<p class="path">
			Candidates: <code>{candidatePath}</code><br />
			Labels: <code>{labelPath}</code>
		</p>
	{/if}

	{#if error}
		<div class="banner err">Patient evidence calibration failed: <code>{error}</code></div>
	{/if}
	{#if savedMessage}
		<div class="banner ok">{savedMessage}</div>
	{/if}

	{#if rows.length === 0 && !loading}
		<p class="empty">
			No patient-evidence candidates loaded. Generate the packet with
			<code>scripts/build_patient_evidence_calibration.py</code>.
		</p>
	{:else}
		<div class="cards">
			{#each rows as row (rowKey(row))}
				{@const current = labels[rowKey(row)]}
				<article class="card">
					<header>
						<div>
							<span class="bucket">{row.candidate_bucket}</span>
							<strong>{row.pair_id}</strong>
							<span class="muted">criterion #{row.criterion_index}</span>
						</div>
						<div class="tags">
							<span>{row.criterion_kind}</span>
							<span>{row.eval_slice}</span>
							<span>{row.matcher_assumption_mode}</span>
							<span>{row.polarity}</span>
							<span>{row.matcher_verdict}</span>
							<span>{row.matcher_reason}</span>
						</div>
					</header>

					<div class="main-grid">
						<section class="candidate">
							<h3>Candidate</h3>
							<p class="criterion">{row.criterion_source_text}</p>
							{#if reviewHint(row)}
								<p class="review-hint">{reviewHint(row)}</p>
							{/if}
							{#if row.composite_groups.length}
								<div class="composite-items">
									{#each row.composite_groups as group (group.group_id)}
										<div class="composite-heading">
											Composite group
											<span>{group.operator === 'any_of' ? 'any of' : 'all of'}</span>
										</div>
										<ol>
											{#each group.subchecks as subcheck (subcheck.subcheck_id)}
												<li>
													<strong>{subcheck.source_text}</strong>
													{#if subcheckRows(row, subcheck).length}
														<ul class="subcheck-evidence">
															{#each subcheckRows(row, subcheck) as sourceRow (sourceRow.row_id)}
																<li>
																	<span class="row-id">{sourceRow.row_id}</span>
																	{sourceRow.label}: {sourceRow.value}
																</li>
															{/each}
														</ul>
													{:else}
														<small>No subcheck-specific evidence retrieved yet.</small>
													{/if}
												</li>
											{/each}
										</ol>
										{/each}
									<p>
										Review these as subchecks, but label the parent criterion until matcher rollup
										supports composite groups.
									</p>
								</div>
							{/if}
							<dl>
								<div>
									<dt>Matcher</dt>
									<dd>
										<strong>{row.matcher_verdict}</strong> · {row.matcher_reason}
									</dd>
								</div>
								<div>
									<dt>Rationale</dt>
									<dd>{row.matcher_rationale}</dd>
								</div>
								{#if row.judge_label}
									<div>
										<dt>Judge</dt>
										<dd>
											{row.judge_label}
											{#if row.judge_error_categories.length}
												· {row.judge_error_categories.join(', ')}
											{/if}
										</dd>
									</div>
								{/if}
							</dl>
							{#if row.matcher_evidence.length}
								<details>
									<summary>Matcher evidence JSON</summary>
									<pre>{JSON.stringify(row.matcher_evidence, null, 2)}</pre>
								</details>
							{/if}
						</section>

						<section class="labeler">
							<h3>Human Label</h3>
							<div class="label-options">
								<label>
									<input
										type="radio"
										name={`evidence-${rowKey(row)}`}
										checked={current?.label === 'supports_present'}
										onchange={() => setEvidenceLabel(row, 'supports_present')}
									/>
									supports present
								</label>
								<label>
									<input
										type="radio"
										name={`evidence-${rowKey(row)}`}
										checked={current?.label === 'supports_absent'}
										onchange={() => setEvidenceLabel(row, 'supports_absent')}
									/>
									supports absent
								</label>
								<label>
									<input
										type="radio"
										name={`evidence-${rowKey(row)}`}
										checked={current?.label === 'supports_measurement_comparison'}
										onchange={() => setEvidenceLabel(row, 'supports_measurement_comparison')}
									/>
									supports measurement comparison
								</label>
								<label>
									<input
										type="radio"
										name={`evidence-${rowKey(row)}`}
										checked={current?.label === 'insufficient_evidence'}
										onchange={() => setEvidenceLabel(row, 'insufficient_evidence')}
									/>
									insufficient evidence
								</label>
							</div>

							<label>
								<span>Matcher assumption</span>
								<select
									value={current?.matcher_assumption_mode ?? row.matcher_assumption_mode}
									onchange={(event) => {
										setAssumptionMode(
											row,
											(event.currentTarget as HTMLSelectElement).value as MatcherAssumptionMode
										);
									}}
								>
									<option value="open_world">open world</option>
									<option value="closed_world_eval">closed world eval</option>
									<option value="closed_world_demo">closed world demo</option>
								</select>
								{#if assumptionGuidance(row, current)}
									<small class:warning={(current?.matcher_assumption_mode ?? row.matcher_assumption_mode) !== 'open_world'}>
										{assumptionGuidance(row, current)}
									</small>
								{/if}
							</label>

							<label>
								<span>Expected matcher verdict</span>
								<select
									value={current?.expected_matcher_verdict ?? ''}
									onchange={(event) => {
										const value = (event.currentTarget as HTMLSelectElement).value;
										setExpectedVerdict(
											row,
											value === ''
												? null
												: (value as PatientEvidenceHumanLabel['expected_matcher_verdict'])
										);
									}}
								>
									<option value="">not set</option>
									<option value="pass">pass</option>
									<option value="fail">fail</option>
									<option value="indeterminate">indeterminate</option>
								</select>
							</label>

							<label>
								<span>Reviewer rationale</span>
								<textarea
									value={current?.rationale ?? ''}
									oninput={(event) =>
										setRationale(row, (event.currentTarget as HTMLTextAreaElement).value)}
									placeholder="What source rows support the label, or why evidence is insufficient?"
								></textarea>
							</label>
						</section>
					</div>

					<section class="sources">
						<div class="source-pane">
							<h3>
								Patient Rows
								<span>{sourceRows(row, 'patient').length}/{sourceRowCount(row, 'patient')}</span>
							</h3>
							<div class="source-table">
								{#if sourceRows(row, 'patient').length === 0}
									<p class="no-matches">No patient rows match the current search.</p>
								{/if}
								{#each sourceRows(row, 'patient') as sourceRow (sourceRow.row_id)}
									<label class="source-row" class:suggested={isRetrieved(row, sourceRow)}>
										<input
											type="checkbox"
											checked={current?.cited_source_row_ids.includes(sourceRow.row_id) ?? false}
											onchange={() => toggleCitation(row, sourceRow.row_id)}
										/>
										<span class="row-id">{sourceRow.row_id}</span>
										<span class="source-main">
											<strong>{sourceRow.label}</strong>
											<span>{sourceRow.value}</span>
											{#if rowSummary(sourceRow)}
												<small>{rowSummary(sourceRow)}</small>
											{/if}
											{#if retrievalReasons(row, sourceRow).length}
												<small class="retrieval">
													{retrievalReasons(row, sourceRow).join(', ')}
												</small>
											{/if}
										</span>
									</label>
								{/each}
							</div>
						</div>

						<div class="source-pane">
							<h3>
								Trial Rows
								<span>{sourceRows(row, 'trial').length}/{sourceRowCount(row, 'trial')}</span>
							</h3>
							<div class="source-table">
								{#if sourceRows(row, 'trial').length === 0}
									<p class="no-matches">No trial rows match the current search.</p>
								{/if}
								{#each sourceRows(row, 'trial') as sourceRow (sourceRow.row_id)}
									<label class="source-row" class:suggested={isRetrieved(row, sourceRow)}>
										<input
											type="checkbox"
											checked={current?.cited_source_row_ids.includes(sourceRow.row_id) ?? false}
											onchange={() => toggleCitation(row, sourceRow.row_id)}
										/>
										<span class="row-id">{sourceRow.row_id}</span>
										<span class="source-main">
											<strong>{sourceRow.label}</strong>
											<span>{sourceRow.value}</span>
											{#if rowSummary(sourceRow)}
												<small>{rowSummary(sourceRow)}</small>
											{/if}
											{#if retrievalReasons(row, sourceRow).length}
												<small class="retrieval">
													{retrievalReasons(row, sourceRow).join(', ')}
												</small>
											{/if}
										</span>
									</label>
								{/each}
							</div>
						</div>
					</section>
				</article>
			{/each}
		</div>
	{/if}
</section>

<style>
	.patient-evidence {
		max-width: 1180px;
	}
	.top {
		display: flex;
		justify-content: space-between;
		gap: 16px;
		align-items: flex-start;
		margin-bottom: 16px;
	}
	.top h2 {
		margin: 0 0 4px 0;
	}
	.top p,
	.path,
	.empty,
	.muted {
		color: #64748b;
	}
	.top p,
	.path {
		margin: 0;
		font-size: 0.9rem;
	}
	.progress {
		padding: 10px 12px;
		border-radius: 10px;
		background: #f8fafc;
		border: 1px solid #e2e8f0;
		text-align: right;
		min-width: 96px;
	}
	.progress strong {
		font-size: 1.4rem;
	}
	.progress-label {
		display: block;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: #64748b;
	}
	.controls {
		display: grid;
		grid-template-columns: minmax(180px, 240px) minmax(260px, 1fr) auto auto;
		gap: 12px;
		align-items: end;
		margin-bottom: 12px;
	}
	.search input {
		width: 100%;
	}
	label span {
		display: block;
		font-size: 0.75rem;
		font-weight: 600;
		color: #475569;
		margin-bottom: 4px;
	}
	label small {
		display: block;
		margin-top: 4px;
		font-size: 0.73rem;
		line-height: 1.35;
		color: #64748b;
	}
	label small.warning {
		color: #92400e;
	}
	input,
	select,
	textarea {
		padding: 6px 10px;
		border: 1px solid #cbd5e1;
		border-radius: 8px;
		font: inherit;
		background: #fff;
	}
	textarea {
		min-height: 86px;
		resize: vertical;
	}
	button {
		border: 0;
		border-radius: 8px;
		padding: 8px 12px;
		background: #e2e8f0;
		color: #0f172a;
		font-weight: 700;
		cursor: pointer;
	}
	button.save {
		background: #2563eb;
		color: white;
	}
	button:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}
	.banner {
		margin: 12px 0;
		padding: 10px 12px;
		border-radius: 8px;
	}
	.banner.err {
		background: #fef2f2;
		color: #991b1b;
		border: 1px solid #fecaca;
	}
	.banner.ok {
		background: #f0fdf4;
		color: #166534;
		border: 1px solid #86efac;
	}
	.cards {
		display: flex;
		flex-direction: column;
		gap: 14px;
		margin-top: 16px;
	}
	.card {
		border: 1px solid #e2e8f0;
		border-radius: 12px;
		padding: 14px;
		background: white;
	}
	.card header,
	.tags {
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
		border-radius: 999px;
		padding: 2px 8px;
		font-size: 0.75rem;
		font-weight: 700;
		background: #eff6ff;
		color: #1e3a8a;
	}
	.tags span {
		background: #f1f5f9;
		color: #334155;
	}
	.main-grid,
	.sources {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
	}
	.candidate,
	.labeler,
	.source-pane {
		min-width: 0;
	}
	h3 {
		margin: 0 0 8px 0;
		font-size: 0.95rem;
	}
	h3 span {
		color: #64748b;
		font-size: 0.78rem;
		font-weight: 500;
		margin-left: 6px;
	}
	.criterion {
		margin: 0 0 10px 0;
		white-space: pre-wrap;
		font-weight: 600;
	}
	.review-hint {
		margin: 0 0 10px 0;
		padding-left: 10px;
		border-left: 3px solid #7c3aed;
		color: #4338ca;
		font-size: 0.86rem;
		line-height: 1.4;
	}
	.composite-items {
		margin: 0 0 12px 0;
		padding: 10px 12px;
		border: 1px solid #bfdbfe;
		border-radius: 10px;
		background: #eff6ff;
		color: #1e3a8a;
	}
	.composite-heading {
		font-size: 0.78rem;
		font-weight: 800;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.composite-heading span {
		margin-left: 6px;
		color: #2563eb;
	}
	.composite-items ol {
		margin: 8px 0;
		padding-left: 20px;
	}
	.composite-items li {
		margin-bottom: 4px;
		color: #1e293b;
	}
	.subcheck-evidence {
		margin: 4px 0 8px 0;
		padding-left: 0;
		list-style: none;
	}
	.subcheck-evidence li {
		font-size: 0.78rem;
		color: #475569;
	}
	.composite-items p {
		margin: 0;
		font-size: 0.8rem;
		color: #475569;
	}
	dl {
		margin: 0;
		display: grid;
		gap: 8px;
	}
	dl div {
		display: grid;
		grid-template-columns: 96px 1fr;
		gap: 8px;
	}
	dt {
		color: #64748b;
		font-size: 0.75rem;
		text-transform: uppercase;
	}
	dd {
		margin: 0;
		color: #334155;
	}
	details {
		margin-top: 10px;
	}
	pre {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		background: #f8fafc;
		border-radius: 8px;
		padding: 10px;
		font-size: 0.78rem;
	}
	.labeler {
		display: grid;
		gap: 10px;
	}
	.label-options {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 8px;
	}
	.label-options label,
	.source-row {
		display: flex;
		align-items: flex-start;
		gap: 8px;
	}
	.sources {
		margin-top: 14px;
		padding-top: 12px;
		border-top: 1px solid #e2e8f0;
	}
	.source-table {
		max-height: 340px;
		overflow: auto;
		border: 1px solid #e2e8f0;
		border-radius: 8px;
	}
	.source-row {
		padding: 8px 10px;
		border-bottom: 1px solid #e2e8f0;
	}
	.source-row.suggested {
		background: #f0fdf4;
		border-left: 3px solid #16a34a;
	}
	.source-row:last-child {
		border-bottom: 0;
	}
	.no-matches {
		margin: 0;
		padding: 10px;
		color: #64748b;
		font-size: 0.85rem;
	}
	.row-id {
		font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono',
			monospace;
		font-size: 0.72rem;
		color: #64748b;
		min-width: 78px;
	}
	.source-main {
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.source-main strong,
	.source-main span,
	.source-main small {
		display: block;
	}
	.source-main span,
	.source-main small {
		color: #475569;
	}
	.source-main small {
		font-size: 0.73rem;
	}
	.source-main small.retrieval {
		color: #166534;
		font-weight: 700;
	}
	@media (max-width: 900px) {
		.top,
		.card header {
			display: block;
		}
		.controls,
		.main-grid,
		.sources {
			grid-template-columns: 1fr;
		}
		.label-options {
			grid-template-columns: 1fr;
		}
	}
</style>
