# Optimization Playbook

Catalog of structure-level model changes used to generate proposals. Every
entry names: the structural template (before / after), the hardware evidence
that makes it applicable, the accuracy risk, and public references.

Training paradigm note: every variant trains FROM SCRATCH at a fixed seed —
no weight is ever inherited, so a change may freely alter the parameter set.
The accuracy risk grades below assume a fresh training run recovers (or
fails to recover) the metric, not a fine-tune.

Scope of this version: six evidence-gated levers — the three conservative
levers (activation replacement, normalization structure, zero-parameter
relocation) plus from-scratch capacity redistribution, projection factorization,
and attention/score-path restructuring. Depth/width and parameterized
relocations are no longer forbidden merely because parameters change; every
entry still requires a measured bottleneck trigger and an explicit
accuracy-risk statement.

## How to use this playbook

1. Read the current bottleneck report (`base/bottleneck_report.json`):
   - `hot_patterns` — critical-path op clusters: pattern id, op type, count,
     total cycles, share of the critical path, and the onnx node names / task
     ids of every op in the cluster;
   - `cost_table` — mean cycles per op type and shape class over the whole
     model;
   - `critical_path` / `pipeline_breakdown` — where the latency chain runs.
2. Match clusters to entries below. An entry applies when its trigger ops
   appear in a hot pattern with a meaningful share of the critical path, and
   the surrounding source confirms the expected structure.
3. Open the current shadow model source under `shadow/`, locate the module
   sites behind the listed onnx node names (exported node names usually carry
   the module path), and count how many sites the change would touch.
4. Derive the op delta per site from the entry's export-pattern table, then
   VERIFY the per-site pattern against the actual base onnx
   (`base/model.onnx`) around those node names — export decomposition varies
   with framework version; the table lists typical opset-17 forms, the actual
   graph is the truth. The op delta = (per-site removal pattern + per-site
   insertion pattern) x site count.
5. Feed the op delta to the cycle predictor, together with the affected
   sites' shape classes (from `base/profile/taskgraph.json`: element count =
   product of the site row's `output_dimensions`, bucketed by the
   cost_table's shape-class labels) so the prediction prices the actual
   shape-class rows. Only strictly negative predictions are admissible; if
   the inserted op type (or one of its shape classes) is absent from the
   cost table, pass an explicit per-op cost override derived from the closest
   same-class row of the cost table and record that derivation in
   `prediction_basis`.

**Count from the actual graph — pinned discipline**: modern exporters at
opset 17 frequently FUSE a decomposition chain into ONE node
(`LayerNormalization` and `Softmax` are the common fused forms; fused
activation nodes such as `Gelu` may appear as well). Every entry below
therefore carries BOTH trigger forms — the decomposed chain AND the fused
single operator — and the op delta ALWAYS counts the operators the actual
exported graph shows, never the theoretical decomposition chain of the
module. A fused node is matched by its op type at the site's position in the
source, not by the chain shape.

Accuracy-risk grades: `low` (mathematically identical or recovered almost
always), `medium` (usually recovered by a short proxy training), `high` (may
need the full training budget or may not recover).

---

## Lever 1 — Activation replacement

Activations are parameter-free: replacing one never changes any parameter.

Hardware rationale shared by all entries: transcendental functions (erf,
tanh, sigmoid-family, exp, sqrt, division) execute at a low per-element rate
on most hardware, while simple elementwise ops (compare, multiply, add) run
several times faster per element. When activation clusters sit on the
critical path, swapping them replaces transcendental work with cheap
elementwise work and shortens the dependency chain per site.

**Fused-form note for this lever**: `Relu`, `Hardswish` and `Sigmoid` already
export as single operators, so their patterns above are the actual-graph
form. When the exporter emits a fused activation node instead of the
decomposition chain (e.g. one `Gelu` node per site), the same entry applies
with the fused form: op delta removes `{Gelu: N}` and inserts the
replacement's single op (`{Relu: N}`) — count what the graph shows.

### A1. GELU -> ReLU

- **Template**: `self.act = nn.GELU()` -> `self.act = nn.ReLU()` (or the
  functional form `F.gelu(x)` -> `F.relu(x)`), applied at every listed site.
- **Evidence**: hot pattern whose ops are the GELU decomposition — the erf
  form exports as an `Erf` plus a small chain of `Div`/`Add`/`Mul`; the tanh
  approximation exports as `Tanh` plus `Mul`/`Add`. Confirm by inspecting the
  base onnx around the listed node names.
- **Export pattern per site (typical, opset 17)**: erf form removes about
  `{Erf: 1, Div: 1, Add: 1, Mul: 2}` and inserts `{Relu: 1}`; tanh form
  removes about `{Tanh: 1, Mul: 3, Add: 1}` and inserts `{Relu: 1}`. Count
  from the actual graph, not from this table.
- **Accuracy risk**: medium. GELU and ReLU differ most for strongly negative
  inputs; a fresh training at the proxy budget usually recovers most of the
  metric.
- **References**: ReLU as the long-standing default activation (Glorot et
  al., 2011); modern efficiency-oriented trainings routinely fall back to
  ReLU-class activations for throughput.

### A2. GELU / ReLU -> square-ReLU

- **Template**: `nn.GELU()` -> `nn.ReLU() ** 2` (equivalently
  `F.relu(x).square()` / an `x * F.relu(x)` formulation — keep one canonical
  form in the edit).
- **Evidence**: same GELU clusters as A1; prefer over A1 when a strictly
  non-monotonic bump near the negative axis matters for the model family
  (square-ReLU keeps a nonzero gradient region on the positive side only, so
  grade the risk slightly above A1).
- **Export pattern per site**: as A1 plus one extra `Mul` on the insert side
  (`{Relu: 1, Mul: 1}` inserted).
- **Accuracy risk**: medium.
- **References**: square-ReLU popularized by single-GPU "cramming" training
  studies (2022) and used by sub-billion-parameter mobile language models
  (MobileLLM, 2024).

### A3. GELU / SiLU -> hard-swish

- **Template**: `nn.GELU()` / `nn.SiLU()` -> `nn.Hardswish()`.
- **Evidence**: GELU or SiLU (`Sigmoid` + `Mul` decomposition) clusters on
  the critical path. `HardSwish` is a single op at opset 17 when exported
  directly; some framework versions decompose it into `Mul + Add + Relu +
  Mul` — check the exported graph and use whichever form the export actually
  produces for the op delta.
- **Accuracy risk**: medium. hard-swish was designed for efficient mobile
  convnets; on attention blocks it usually trains back well from scratch.
- **References**: MobileNetV3 (Howard et al., 2019).

### A4. SiLU / Swish -> ReLU

- **Template**: `nn.SiLU()` -> `nn.ReLU()` (also covers `x * sigmoid(x)`
  functional forms).
- **Evidence**: `Sigmoid` + `Mul` clusters at activation positions; sigmoid
  is transcendental-rate work, ReLU is a single cheap compare.
- **Export pattern per site**: removes about `{Sigmoid: 1, Mul: 1}`, inserts
  `{Relu: 1}`.
- **Accuracy risk**: medium.
- **References**: Swish/SiLU (Ramachandran et al., 2017; Elfwing et al.,
  2018) reports small gains over ReLU at full training budgets — the gain is
  usually not latency-worthy when the activation is the bottleneck.

### A5. Hidden activation -> sigmoid

- **Template**: any hidden activation -> `nn.Sigmoid()`.
- **Evidence**: only consider when A1-A4 are inapplicable and the cost table
  shows `Sigmoid` meaningfully cheaper than the current op mix.
- **Accuracy risk**: high. Sigmoid saturates on both sides; hidden-layer use
  is mostly historical. Propose only when nothing cheaper remains.
- **References**: classical sigmoid networks; modern usage is essentially
  confined to gates.

---

## Lever 2 — Normalization structure

Entries in this lever may add, remove, or reshape parameters — the
from-scratch training paradigm makes any parameter-set change viable; the
op delta (counted from the actual exported graph) is the only declaration
that must match reality.

Hardware rationale shared by all entries: a normalization layer exports as a
reduction chain (`ReduceMean`/`ReduceSum` + `Pow` + `Sqrt` + `Div`) plus
parameterized elementwise ops, and it sits mid-critical-path as a full
dependency barrier (every output element depends on every input element of
the row). Removing or cheapening normalization removes reduction + division
work and shortens the chain.

### N1. LayerNorm -> RMSNorm

- **Template**: `nn.LayerNorm(d)` -> RMS normalization (mean subtraction and
  bias removed; scale kept): `x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True)
  + eps) * weight`.
- **Evidence**: LayerNorm clusters. Decomposed export form: the export
  typically contains `ReduceMean` (twice: mean and variance), `Sub`, `Pow`,
  `Sqrt`, `Div`, then `Mul` (weight) and `Add` (bias). Fused export form (the
  common opset-17 shape): one `LayerNormalization` node per site carries the
  whole computation — trigger on `LayerNormalization` clusters in that case.
- **Export pattern per site (typical)**: decomposed form removes roughly
  `{ReduceMean: 2, Sub: 1, Add: 1}` keeping `{Pow: 1, ReduceMean: 1, Sqrt: 1,
  Div: 1, Mul: 2}`; fused form removes `{LayerNormalization: 1}` and inserts
  the RMSNorm decomposition chain — count from the actual graph (framework
  export may fuse or split these).
- **Accuracy risk**: medium-low; RMSNorm is the standard normalization in
  current open language models and typically matches LayerNorm when trained
  from scratch.
- **References**: RMSNorm (Zhang & Sennrich, 2019); adopted as-is by the
  LLaMA / T5 / Gemma model families.

### N2. Remove redundant normalization

- **Template**: delete a normalization module that is provably redundant —
  immediately followed by another normalization of the same tensor, or
  applied to a branch whose result is only consumed in scale-invariant ways
  (wire the input straight to the consumer). Redundant double normalization
  (`normalize(normalize(x))`) collapses to a single normalization.
- **Evidence**: repeated `ReduceL2`/`Sqrt`/`Div` (or `ReduceSum` + `Pow`) rows
  chained on the critical path — common in similarity / retrieval heads and
  QK-normalized attention. Read the source around the listed node names and
  verify the redundancy actually holds; if the per-sample norm genuinely
  feeds magnitude-sensitive computation, the entry does not apply.
- **Export pattern per site**: removes the norm's reduction chain (or its
  fused single-op form, when the exporter emitted one) and inserts nothing —
  count from the actual graph.
- **Accuracy risk**: low when exactly redundant; medium when the removal
  changes activation magnitudes entering a region that is not
  scale-normalized afterwards.
- **References**: normalization-free residual architectures (NFNet, Brock et
  al., 2021) show trained networks can operate without many normalization
  layers when rescaling is handled.

### N3. Fused LayerNorm removal (delete the LayerNormalization nodes)

- **Template**: a `nn.LayerNorm(d)` directly feeding a linear layer is
  REMOVED entirely — delete the LayerNormalization node(s)/module and wire
  the un-normalized input straight through (`linear(normalize(x))` becomes
  `linear(x)`). No parameter folding is needed: the from-scratch retraining
  re-learns the adjacent weights anyway; the norm's own parameters simply
  disappear.
- **Evidence (fused single-op form)**: `LayerNormalization` clusters at
  opset 17 — the exporter fused the decomposition chain into one node per
  site. The decomposed form of the same structure also applies; count the
  operators the graph actually shows.
- **Export pattern per site (fused form)**: removes
  `{LayerNormalization: 1}`, inserts nothing.
- **Accuracy risk**: medium — the bet is that a fresh training run absorbs
  the missing per-sample normalization statistics. Rank after the
  exact-identity entries (C2, N2-exact); the proxy stage exists to judge
  exactly this kind of change.
- **References**: normalization-free training literature (NFNet, Brock et
  al., 2021) motivates removing the normalization itself; current open
  models show the affine-free path trains fine from scratch.

---

## Lever 3 — Compute relocation

This lever relocates or cancels computation. The two entries here remain
zero-parameter; parameterized factorization has its own lever below.

### C1. Softmax attention scores -> ReLU-score variant

- **Template**: attention score computation
  `softmax(Q @ K^T / sqrt(d))` -> `relu(Q @ K^T / sqrt(d))` (optionally with
  a constant row rescale that is itself parameter-free; do not add learned
  parameters).
- **Evidence**: `Softmax` clusters at attention positions. Fused export form
  (the common opset-17 shape): ONE `Softmax` node per site. Decomposed form:
  a max-reduction + `Sub` + `Exp` + sum-reduction + `Div` — two reductions
  and transcendental work per site. In both forms the ReLU variant is a
  single cheap elementwise op.
- **Export pattern per site**: fused form removes `{Softmax: 1}` and inserts
  `{Relu: 1}` (so N sites give an op delta of
  `{Softmax: -N, Relu: +N}`); decomposed form removes about
  `{ReduceMax: 1, Sub: 1, Exp: 1, ReduceSum: 1, Div: 1}` and inserts
  `{Relu: 1}` — count from the actual graph (frameworks may fuse or split
  the max path).
- **Accuracy risk**: high. Score distributions change materially; this entry
  needs the full training budget to be judged and should be ranked after the
  activation / normalization levers.
- **Parameter impact**: none — attention scores carry no parameters.
- **References**: kernel-replacement attention (Performer, Choromanski et
    al., 2020) and ReLU-score attention ablations in the efficient-attention
    literature.

### C2. Cancel redundant op pairs

- **Template**: remove pairs that undo each other: a transpose (or reshape)
  immediately followed by its inverse, a dtype cast round-trip, a
  concatenation immediately split back apart. Replace the pair with a direct
  wiring of the input to the consumer.
- **Evidence**: `Transpose`/`Reshape`/`Cast`/`Concat`/`Split` op pairs on
  the critical path whose output shape equals the original input shape; data
  movement is pure memory traffic with no reuse of computed values.
- **Export pattern per site**: removes the pair's two ops, inserts nothing.
- **Accuracy risk**: low — mathematically identical when the pair truly
  cancels; verify shapes on paper before declaring it.
- **Parameter impact**: none.
- **References**: redundant-op cancellation is a standard graph-level
  optimization; here it is done at the model-source level so it survives
  export.

---

## Lever 4 — Capacity redistribution

These entries change width/depth while preserving the model's input/output
contract. They are justified only when profiling shows that a particular
matrix-multiply shape class is latency-dominant and the target hardware prices
tall/narrow or short/wide shapes inefficiently.

### D1. Deeper-narrower block

- **Template**: replace a block whose dominant `MatMul` output shape is wide
  with narrower blocks whose total FLOP capacity is close to the original
  (for example `Linear(d, 4d) -> Linear(4d, d)` becomes two
  `d -> 2d -> d` residual sub-blocks) only when the profile shows the smaller
  shape class is materially cheaper per MAC.
- **Evidence**: hot `MatMul`/`Gemm` rows in a poorly priced shape class plus a
  cost-table ratio showing the narrower decomposition lowers cycles.
- **Accuracy expectation**: `small_negative` with `medium` confidence for a
  near-capacity redistribution; `unknown` when capacity changes materially.
- **References**: MobileNet width multipliers (Howard, 2017) and EfficientNet
  compound scaling (Tan & Le, 2019) show width/depth tradeoffs must be
  retrained, not interpolated.

### D2. Shallower-wider block

- **Template**: collapse repeated shallow blocks into one wider block when
  per-block movement/reduction overhead dominates and the fused shape remains
  in a hardware-efficient cost-table class.
- **Evidence**: repeated subgraph overhead, high movement/reduction share, and
  an actual graph pattern proving consumers allow fusion.
- **Accuracy expectation**: `small_negative`, `medium` confidence.
- **References**: mobile CNN/transformer designs consistently show that
  removing sequential overhead helps only when representational width is
  retained.

---

## Lever 5 — Projection factorization

### F1. Low-rank projection

- **Template**: `Linear(a, b)` -> `Linear(a, r)` + `Linear(r, b)` with `r`
  chosen from a measured shape-class break-even, not by guesswork.
- **Evidence**: the original shape class is latency-dominant and the cost table
  proves the two smaller multiplications plus the intermediate movement are
  cheaper. Do not propose when movement cancels the arithmetic saving.
- **Accuracy expectation**: `small_negative` at mild rank reduction with
  `medium` confidence; `unknown` when `r` removes roughly a third or more of
  the original rank capacity.
- **References**: ALBERT parameter factorization (Lan et al., 2020) and
  low-rank adapter results show accuracy depends on task and retained rank.

### F2. Shared projection

- **Template**: reuse one projection for structurally equivalent branches that
  the business-logic baseline identifies as interchangeable.
- **Evidence**: multiple equivalent `MatMul` clusters with identical input
  shape and no semantics that require independent parameters.
- **Accuracy expectation**: `unknown`, `medium` confidence.
- **References**: ALBERT cross-layer sharing (Lan et al., 2020) is the
  canonical accuracy/latency tradeoff and must be re-measured per task.

---

## Lever 6 — Score-path restructuring

### S1. Linear/low-rank attention score path

- **Template**: replace the exact attention score path only when business
  logic permits an approximation and profiling proves `Softmax` plus score
  matmuls dominate. Allowed forms are low-rank score projection or a
  mathematically stated efficient-attention replacement.
- **Evidence**: `Softmax`, `Transpose`, and score `MatMul` together dominate
  the critical path; the replacement exports to supported operators.
- **Accuracy expectation**: `unknown` with `low` confidence; require a
  coarse-training win before promotion.
- **References**: Performer FAVOR+ (Choromanski et al., 2020), linear
  transformers (Katharopoulos et al., 2020), and FlashAttention's IO-aware
  formulation (Dao et al., 2022) motivate the direction but do not guarantee
  task accuracy.

### S2. Fuse adjacent elementwise/reduction chains

- **Template**: rewrite adjacent elementwise/reduction chains as one model-level
  operation when export emits the fused operator and no semantic intermediate
  is externally consumed.
- **Evidence**: long dependent chains of cheap operators with high combined
  share and scheduling delay.
- **Accuracy expectation**: `none` when mathematically identical; otherwise
  `unknown`.
- **References**: operator fusion and IO-aware execution are standard latency
  techniques; the exported ONNX graph is the final truth.

---

## Proposal admission checklist (mechanical, all must hold)

1. `predicted_delta_cycles < 0` — strictly negative, produced by the cycle
   predictor script, never by mental arithmetic.
2. Every `edited_files` entry is a path inside the current shadow tree (the
   file must exist there before the edit).
3. The op delta is consistent with the change description: per-site pattern
   x site count, verified against the actual base onnx.
4. The change signature is built canonically by the signature builder
   (lever + predictor-generated params + sorted module list) — never
   hand-assembled.
5. `expected_accuracy_impact`, `accuracy_confidence`, `accuracy_evidence`, and
   `sota_ref` are present.

## Accuracy and Pareto ranking contract

Every proposal must carry:

- `expected_accuracy_impact`: `none` | `small_negative` |
  `large_negative` | `unknown`;
- `accuracy_confidence`: `low` | `medium` | `high`;
- `accuracy_evidence`: relevant `experiment_ledger.json` rows, playbook risk,
  and any same-run coarse-curve evidence;
- `sota_ref`: one or more concrete references.

Discard dominated candidates: another candidate has no worse expected accuracy
risk and a larger predicted latency reduction. Rank remaining Pareto
candidates by measured evidence and prediction quality from the experiment
ledger, then by larger predicted reduction and lower accuracy risk. Keep at
most one aggressive `unknown` / `low-confidence` candidate per round when a
conservative candidate is available.

Never treat an SOTA reference as proof of accuracy. It is only candidate
selection evidence: MFU measurement is the latency truth and coarse/full
training is the accuracy truth.
