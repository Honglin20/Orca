# Evaluation Paradigm

The NAS search evaluator uses one fixed paradigm: **`validate`** (zero-training). There is no paradigm override — candidate subnets are never trained or fine-tuned during search, and this pipeline never searches over an untrained supernet.

Each candidate is a per-slot choice path. The evaluator applies `set_sample_config` with the choice-only config and runs direct inference on the supernet's inherited weights: variant branches carry the KD-trained weights, original branches and non-slot modules carry the frozen pretrained weights. Inherited weights are directly evaluable, which makes zero-training validation the default and only mode.

The reported quality metric is the user's original validation metric, computed by the ported evaluation entry. KD losses and the teacher never enter the search objective.
