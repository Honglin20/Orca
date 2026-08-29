# Structural Levers Reference

Catalog of structure-level model changes, used as BACKGROUND PRIORS by the
proposal stage. Every entry names: the structural template (before / after),
the hardware evidence that makes it applicable, the accuracy risk, and public
references. The evidence you actually reason from is the current run's
bottleneck analysis + business-logic document — this catalog tells you what
kinds of structure changes exist, not which one to pick this round.

Training paradigm note: every variant trains FROM SCRATCH at a fixed seed —
no weight is ever inherited, so a change may freely alter the parameter set.
The accuracy risk grades below assume a fresh training run recovers (or
fails to recover) the metric, not a fine-tune.

Hard scope note: these are all MODEL-SOURCE changes. Training hyperparameters
(learning rate schedule, optimizer settings, epochs) are not a lever here —
the training entry is contract-templated and physically outside the shadow
closure an edit can reach, and a hyperparameter "change" produces an empty
op delta that the strictly-negative admission gate rejects anyway.

This catalog covers six evidence-gated lever families — the three
conservative ones (activation replacement, normalization structure,
zero-parameter relocation) plus capacity redistribution, projection
factorization, and attention/score-path restructuring.

## How to use this reference

1. Read the current bottleneck analysis (`base/bottleneck_analysis.json` —
   the semantic selection) and the mechanical report behind it
   (`base/bottleneck_report.json`): `hot_patterns`, `cost_table`,
   `critical_path`, `pipeline_breakdown`.
2. Match selected bottlenecks to entries below. An entry applies when its
   trigger ops appear in the bottleneck's pattern with a meaningful share,
   AND the business-logic document (`baseline/business_logic.md`) confirms
   the surrounding semantics allow the change.
3. Open the current shadow model source under `shadow/`, locate the module
   sites behind the listed onnx node names, and count how many sites the
   change would touch.
4. Derive the op delta per site from the entry's export-pattern table, then
   VERIFY the per-site pattern against the actual base onnx
   (`base/model.onnx`) — export decomposition varies with framework
   version; the actual graph is the truth. Op delta = (per-site removal +
   per-site insertion) x site count.
5. Feed the op delta to the cycle predictor with the affected sites' shape
   classes so the prediction prices the actual sites (from
   `base/profile/taskgraph.json`: element count = product of the site row's
   `output_dimensions`, bucketed by the cost_table's shape-class labels).
   Only strictly negative predictions are admissible; when the inserted op
   type is absent from the cost table, pass an explicit per-op cost override
   derived from the closest same-class row and record that derivation in
   `prediction_basis`.

**Count from the actual graph — pinned discipline**: modern exporters at
opset 17 frequently FUSE a decomposition chain into ONE node
(`LayerNormalization` and `Softmax` are the common fused forms; fused
activation nodes such as `Gelu` may appear as well). Every entry below
carries BOTH trigger forms — the decomposed chain AND the fused single
operator — and the op delta ALWAYS counts the operators the actual exported
graph shows.

Accuracy-risk grades: `low` (mathematically identical or recovered almost
always), `medium` (usually recovered by a fresh training), `high` (may need
the full training budget or may not recover).

---

## Lever 1 — Activation replacement

Activations are parameter-free: replacing one never changes any parameter.

Hardware rationale shared by all entries: transcendental functions (erf,
tanh, sigmoid-family, exp, sqrt, division) execute at a low per-element rate
on most hardware, while simple elementwise ops (compare, multiply, add) run
several times faster per element. When activation clusters sit on the
critical path, swapping them replaces transcendental work with cheap
elementwise work and shortens the dependency chain per site.

**Fused-form note**: `Relu`, `Hardswish` and `Sigmoid` already export as
single operators. When the exporter emits a fused activation node (e.g. one
`Gelu` node per site), the same entry applies with the fused form — op delta
removes `{Gelu: N}` and inserts the replacement's single op. Count what the
graph shows.

### A1. GELU -> ReLU

- **Template**: `self.act = nn.GELU()` -> `self.act = nn.ReLU()` (or
  `F.gelu(x)` -> `F.relu(x)`), applied at every listed site.
- **Evidence**: hot pattern whose ops are the GELU decomposition — the erf
  form exports as `Erf` plus a small `Div`/`Add`/`Mul` chain; the tanh
  approximation as `Tanh` plus `Mul`/`Add`.
- **Export pattern per site (typical, opset 17)**: erf form removes about
  `{Erf: 1, Div: 1, Add: 1, Mul: 2}`, inserts `{Relu: 1}`; tanh form
  removes about `{Tanh: 1, Mul: 3, Add: 1}`, inserts `{Relu: 1}`.
- **Accuracy risk**: medium. GELU and ReLU differ most for strongly negative
  inputs; a fresh training usually recovers most of the metric.
- **References**: ReLU as the long-standing default activation (Glorot et
  al., 2011); efficiency-oriented trainings routinely fall back to
  ReLU-class activations for throughput.

### A2. GELU / ReLU -> square-ReLU

- **Template**: `nn.GELU()` -> `nn.ReLU() ** 2` (equivalently
  `F.relu(x).square()` — keep one canonical form in the edit).
- **Evidence**: same GELU clusters as A1; prefer when a non-monotonic bump
  near the negative axis matters for the model family (grade the risk
  slightly above A1).
- **Export pattern per site**: as A1 plus one extra `Mul` on the insert
  side.
- **Accuracy risk**: medium.
- **References**: square-ReLU popularized by single-GPU "cramming" training
  studies (2022) and used by sub-billion-parameter mobile language models
  (MobileLLM, 2024).

### A3. GELU / SiLU -> hard-swish

- **Template**: `nn.GELU()` / `nn.SiLU()` -> `nn.Hardswish()`.
- **Evidence**: GELU or SiLU (`Sigmoid` + `Mul` decomposition) clusters on
  the critical path. `HardSwish` may export as one op or decompose into
  `Mul + Add + Relu + Mul` — use whichever form the export produces.
- **Accuracy risk**: medium.
- **References**: MobileNetV3 (Howard et al., 2019).

### A4. SiLU / Swish -> ReLU

- **Template**: `nn.SiLU()` -> `nn.ReLU()` (also covers `x * sigmoid(x)`
  functional forms).
- **Evidence**: `Sigmoid` + `Mul` clusters at activation positions.
- **Export pattern per site**: removes about `{Sigmoid: 1, Mul: 1}`,
  inserts `{Relu: 1}`.
- **Accuracy risk**: medium.
- **References**: Swish/SiLU (Ramachandran et al., 2017; Elfwing et al.,
  2018) reports small gains over ReLU at full budgets — usually not
  latency-worthy when the activation is the bottleneck.

### A5. Hidden activation -> sigmoid

- **Template**: any hidden activation -> `nn.Sigmoid()`.
- **Evidence**: only when A1-A4 are inapplicable and the cost table shows
  `Sigmoid` meaningfully cheaper than the current op mix.
- **Accuracy risk**: high. Hidden-layer sigmoid use is mostly historical;
  propose only when nothing cheaper remains.
- **References**: classical sigmoid networks; modern usage is essentially
  confined to gates.

---

## Lever 2 — Normalization structure

Entries here may add, remove, or reshape parameters — the from-scratch
training paradigm makes any parameter-set change viable; the op delta
(counted from the actual exported graph) is the only declaration that must
match reality.

Hardware rationale shared by all entries: a normalization layer exports as a
reduction chain (`ReduceMean`/`ReduceSum` + `Pow` + `Sqrt` + `Div`) plus
parameterized elementwise ops, and sits mid-critical-path as a full
dependency barrier. Removing or cheapening normalization removes reduction +
division work and shortens the chain.

### N1. LayerNorm -> RMSNorm

- **Template**: `nn.LayerNorm(d)` -> RMS normalization (mean subtraction and
  bias removed; scale kept): `x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True)
  + eps) * weight`.
- **Evidence**: LayerNorm clusters. Fused export form (the common opset-17
  shape): one `LayerNormalization` node per site; decomposed form:
  `ReduceMean` x2, `Sub`, `Pow`, `Sqrt`, `Div`, `Mul`, `Add`.
- **Export pattern per site (typical)**: decomposed form removes roughly
  `{ReduceMean: 2, Sub: 1, Add: 1}` keeping `{Pow: 1, ReduceMean: 1, Sqrt:
  1, Div: 1, Mul: 2}`; fused form removes `{LayerNormalization: 1}` and
  inserts the RMSNorm decomposition — count from the actual graph.
- **Accuracy risk**: medium-low; RMSNorm is the standard normalization in
  current open language models.
- **References**: RMSNorm (Zhang & Sennrich, 2019); LLaMA / T5 / Gemma.

### N2. Remove redundant normalization

- **Template**: delete a normalization module that is provably redundant —
  immediately followed by another normalization of the same tensor, or on a
  branch whose result is only consumed in scale-invariant ways.
- **Evidence**: repeated `ReduceL2`/`Sqrt`/`Div` (or `ReduceSum` + `Pow`)
  rows chained on the critical path — common in similarity / retrieval heads
  and QK-normalized attention. Verify the redundancy actually holds in the
  source; if the per-sample norm feeds magnitude-sensitive computation, the
  entry does not apply.
- **Export pattern per site**: removes the norm's reduction chain (or its
  fused single-op form), inserts nothing.
- **Accuracy risk**: low when exactly redundant; medium when removal changes
  activation magnitudes entering a non-scale-normalized region.
- **References**: normalization-free residual architectures (NFNet, Brock et
  al., 2021).

### N3. Fused LayerNorm removal

- **Template**: a `nn.LayerNorm(d)` directly feeding a linear layer is
  REMOVED entirely (`linear(normalize(x))` becomes `linear(x)`). The
  from-scratch retraining re-learns the adjacent weights; the norm's own
  parameters disappear.
- **Evidence**: `LayerNormalization` clusters at opset 17 (the fused form of
  the same structure also applies in decomposed form).
- **Export pattern per site (fused form)**: removes
  `{LayerNormalization: 1}`, inserts nothing.
- **Accuracy risk**: medium — rank after the exact-identity entries (C2,
  and N2 when the redundancy is exact); the probe stage exists to judge
  exactly this kind of change.
- **References**: normalization-free training literature (NFNet, Brock et
  al., 2021).

---

## Lever 3 — Compute relocation

### C1. Softmax attention scores -> ReLU-score variant

- **Template**: `softmax(Q @ K^T / sqrt(d))` -> `relu(Q @ K^T / sqrt(d))`
  (optionally a parameter-free constant row rescale; never add learned
  parameters).
- **Evidence**: `Softmax` clusters at attention positions. Fused form: ONE
  `Softmax` node per site; decomposed form: max-reduction + `Sub` + `Exp` +
  sum-reduction + `Div`.
- **Export pattern per site**: fused form removes `{Softmax: 1}`, inserts
  `{Relu: 1}`; decomposed form removes about `{ReduceMax: 1, Sub: 1, Exp:
  1, ReduceSum: 1, Div: 1}`, inserts `{Relu: 1}`.
- **Accuracy risk**: high; rank after the activation / normalization levers.
- **Parameter impact**: none — attention scores carry no parameters.
- **References**: kernel-replacement attention (Performer, Choromanski et
    al., 2020) and ReLU-score attention ablations.

### C2. Cancel redundant op pairs

- **Template**: remove pairs that undo each other: a transpose (or reshape)
  immediately followed by its inverse, a dtype cast round-trip, a
  concatenation immediately split back apart. Wire the input straight to the
  consumer.
- **Evidence**: `Transpose`/`Reshape`/`Cast`/`Concat`/`Split` op pairs on
  the critical path whose output shape equals the original input shape.
- **Export pattern per site**: removes the pair's two ops, inserts nothing.
- **Accuracy risk**: low — mathematically identical when the pair truly
  cancels; verify shapes before declaring it.
- **Parameter impact**: none.
- **References**: redundant-op cancellation is a standard graph-level
  optimization; done at model-source level so it survives export.

---

## Lever 4 — Capacity redistribution

These entries change width/depth while preserving the model's input/output
contract. Justified only when profiling shows a matrix-multiply shape class
is latency-dominant and the target hardware prices tall/narrow or
short/wide shapes inefficiently.

### D1. Deeper-narrower block

- **Template**: replace a block whose dominant `MatMul` output shape is wide
  with narrower blocks of near-equal total capacity (e.g. `Linear(d, 4d) ->
  Linear(4d, d)` becomes two `d -> 2d -> d` residual sub-blocks) when the
  profile shows the smaller shape class is materially cheaper per MAC.
- **Evidence**: hot `MatMul`/`Gemm` rows in a poorly priced shape class plus
  a cost-table ratio showing the narrower decomposition lowers cycles.
- **Accuracy expectation**: `small_negative` with `medium` confidence for a
  near-capacity redistribution; `unknown` when capacity changes materially.
- **References**: MobileNet width multipliers (Howard, 2017); EfficientNet
  compound scaling (Tan & Le, 2019).

### D2. Shallower-wider block

- **Template**: collapse repeated shallow blocks into one wider block when
  per-block movement/reduction overhead dominates and the fused shape stays
  hardware-efficient.
- **Evidence**: repeated subgraph overhead, high movement/reduction share,
  and an actual graph pattern proving consumers allow fusion.
- **Accuracy expectation**: `small_negative`, `medium` confidence.
- **References**: mobile CNN/transformer designs: removing sequential
  overhead helps only when representational width is retained.

---

## Lever 5 — Projection factorization

### F1. Low-rank projection

- **Template**: `Linear(a, b)` -> `Linear(a, r)` + `Linear(r, b)` with `r`
  from a measured shape-class break-even, not guesswork.
- **Evidence**: the original shape class is latency-dominant and the cost
  table proves the two smaller multiplications plus the intermediate
  movement are cheaper. Do not propose when movement cancels the saving.
- **Accuracy expectation**: `small_negative` at mild rank reduction,
  `medium` confidence; `unknown` when `r` removes a third or more of the
  original rank.
- **References**: ALBERT parameter factorization (Lan et al., 2020).

### F2. Shared projection

- **Template**: reuse one projection for structurally equivalent branches
  the business-logic document identifies as interchangeable.
- **Evidence**: multiple equivalent `MatMul` clusters with identical input
  shape and no semantics requiring independent parameters.
- **Accuracy expectation**: `unknown`, `medium` confidence.
- **References**: ALBERT cross-layer sharing (Lan et al., 2020).

---

## Lever 6 — Score-path restructuring

### S1. Linear/low-rank attention score path

- **Template**: replace the exact attention score path only when the
  business logic permits an approximation and profiling proves `Softmax`
  plus score matmuls dominate. Allowed forms: low-rank score projection or a
  mathematically stated efficient-attention replacement.
- **Evidence**: `Softmax`, `Transpose`, and score `MatMul` dominating the
  critical path; the replacement exports to supported operators.
- **Accuracy expectation**: `unknown` with `low` confidence; require a
  probe win before promotion.
- **References**: Performer FAVOR+ (Choromanski et al., 2020), linear
  transformers (Katharopoulos et al., 2020), FlashAttention's IO-aware
  formulation (Dao et al., 2022).

### S2. Fuse adjacent elementwise/reduction chains

- **Template**: rewrite adjacent elementwise/reduction chains as one
  model-level operation when export emits the fused operator and no semantic
  intermediate is externally consumed.
- **Evidence**: long dependent chains of cheap operators with high combined
  share and scheduling delay.
- **Accuracy expectation**: `none` when mathematically identical; otherwise
  `unknown`.
- **References**: operator fusion and IO-aware execution are standard
  latency techniques; the exported ONNX graph is the final truth.

---

## Proposal admission checklist (mechanical, all must hold)

1. `predicted_delta_cycles < 0` — strictly negative, produced by the cycle
   predictor script, never by mental arithmetic.
2. Every `edited_files` entry is a path inside the current shadow tree.
3. The op delta is consistent with the change description: per-site pattern
   x site count, verified against the actual base onnx.
4. The change signature is built canonically by the signature builder —
   never hand-assembled.
5. Accuracy-risk fields (`expected_accuracy_impact`,
   `accuracy_confidence`, `accuracy_evidence`) and `sota_reference` are
   present.

## Accuracy and Pareto ranking contract

Every proposal carries: `expected_accuracy_impact` (`none` /
`small_negative` / `large_negative` / `unknown`), `accuracy_confidence`
(`low` / `medium` / `high`), `accuracy_evidence` (ledger rows, lever priors,
same-run curve evidence), `sota_reference` (concrete references).

Discard dominated candidates: another candidate with no worse expected
accuracy risk and a larger predicted latency reduction dominates. Rank
remaining Pareto candidates by measured evidence from the experiment
ledger, then by larger predicted reduction and lower accuracy risk. Keep at
most one aggressive `unknown` / `low-confidence` candidate per round when a
conservative candidate is available.

Never treat an SOTA reference as proof of accuracy: profiling is the latency
truth, training is the accuracy truth.
