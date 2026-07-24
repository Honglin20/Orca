# Evaluation Paradigm

Choose one paradigm for the NAS search evaluator based on the task type and training context from the user's project <user_project_root>.

**`train_from_scratch`**: Use when supernet training is not viable for the task (e.g. RL, GAN, self-supervised learning with architectural coupling). `train_supernet.py` and `run_train_supernet.sh` are not generated. The search evaluator extracts each subnet, re-initializes weights, trains from scratch, then validates.

**`validate`** (default): Use when supernet training is viable and weight-sharing quality is expected to be reliable:

- Standard supervised tasks (classification, regression, segmentation, detection, language modeling).
- Moderate search space with similar block types within each stage.
- KD enabled during supernet training, which generally improves weight-sharing quality.

**`finetune`**: Use when supernet training is viable but direct evaluation is unreliable due to a large weight-sharing gap or a domain shift between training and evaluation:

- Highly heterogeneous block types in the same searchable layer (e.g. convolution alongside attention) where weight-sharing across structurally different blocks is inherently noisy.
- Pretrained backbone replacement making per-subnet calibration important for quality estimation.
- Large depth or branch variance creating significant capacity gaps between max and min subnets.
- KD not enabled and task loss alone may not provide enough gradient signal to stabilize shared weights.
- Cross-dataset evaluation: the supernet was pretrained on a source dataset (e.g. ImageNet) and the search must rank subnets on a different target dataset (e.g. a domain-specific downstream dataset). Inherited weights provide a strong initialization but the head and feature distribution require short adaptation on the target data before validation scores become meaningful.

