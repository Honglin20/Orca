"""Knowledge distillation utilities."""

import torch
import torch.nn.functional as F
from typing import Optional


def logit_standardization(logits: torch.Tensor) -> torch.Tensor:
    """Standardizes logits along the last dimension to have zero mean and unit variance.

    Args:
        logits (torch.Tensor): The input logits to standardize.

    Returns:
        torch.Tensor: The standardized logits.
    """
    mean = logits.mean(dim=-1, keepdim=True)
    std = logits.std(dim=-1, keepdim=True, unbiased=False)
    return (logits - mean) / (std + 1e-7)


def logits_kd_loss(
    student_output: torch.Tensor,
    teacher_output: torch.Tensor,
    *,
    temperature: float = 1.0,
    use_logit_standardization: bool = True,
    mask: Optional[torch.Tensor] = None,
) -> torch.Tensor:
    """Computes Knowledge Distillation (KD) loss between student and teacher logits.

    Args:
        student_output (torch.Tensor): Logits output by the student model.
        teacher_output (torch.Tensor): Logits output by the teacher model.
        temperature (float, optional): Temperature scaling factor for softening probabilities. Defaults to 1.0.
        use_logit_standardization (bool, optional): Whether to standardize logits before KD. Defaults to True.
        mask (Optional[torch.Tensor], optional): Boolean mask to ignore padding tokens. Defaults to None.

    Returns:
        torch.Tensor: The computed KD loss.

    Raises:
        ValueError: If student and teacher outputs do not have the same shape, or if the mask shape is invalid.
    """
    if student_output.shape != teacher_output.shape:
        raise ValueError(
            "student_output and teacher_output must have the same shape for KD."
        )

    student = student_output
    teacher = teacher_output.detach()
    if use_logit_standardization:
        student = logit_standardization(student)
        teacher = logit_standardization(teacher)

    if mask is not None:
        if mask.shape != student.shape[:-1]:
            raise ValueError("mask must match all output dimensions except the last.")
        valid_mask = mask.to(torch.bool)
        student = student[valid_mask]
        teacher = teacher[valid_mask]
    else:
        student = student.reshape(-1, student.shape[-1])
        teacher = teacher.reshape(-1, teacher.shape[-1])

    return (
        F.kl_div(
            F.log_softmax(student / temperature, dim=-1),
            F.softmax(teacher / temperature, dim=-1),
            reduction="batchmean",
        )
        * temperature**2
    )


def mse_kd_loss(
    student_output: torch.Tensor,
    teacher_output: torch.Tensor,
    *,
    mask: Optional[torch.Tensor] = None,
) -> torch.Tensor:
    """Computes Mean Squared Error (MSE) Knowledge Distillation loss.

    Args:
        student_output (torch.Tensor): Output from the student model.
        teacher_output (torch.Tensor): Output from the teacher model.
        mask (Optional[torch.Tensor], optional): Boolean mask to ignore specific tokens/elements. Defaults to None.

    Returns:
        torch.Tensor: The computed MSE loss.

    Raises:
        ValueError: If student and teacher outputs do not have the same shape, or if the mask shape is invalid.
    """
    if student_output.shape != teacher_output.shape:
        raise ValueError(
            "student_output and teacher_output must have the same shape for KD."
        )

    student = student_output
    teacher = teacher_output.detach()
    if mask is not None:
        valid_mask = mask.to(torch.bool)
        if mask.shape == student.shape:
            student = student[valid_mask]
            teacher = teacher[valid_mask]
        elif mask.shape == student.shape[:-1]:
            student = student[valid_mask]
            teacher = teacher[valid_mask]
        else:
            raise ValueError(
                "mask must match the full output shape or all dimensions except the last."
            )

    return F.mse_loss(student, teacher)


def cosine_kd_loss(
    student_output: torch.Tensor,
    teacher_output: torch.Tensor,
    *,
    mask: Optional[torch.Tensor] = None,
) -> torch.Tensor:
    """Computes Cosine Similarity Knowledge Distillation loss.

    Measures the directional disagreement between student and teacher
    representations by computing `1 - cosine_similarity` averaged over all
    valid positions. Because cosine similarity is scale-invariant, this loss
    is unaffected by differences in feature magnitude between teacher and
    student — making it particularly suitable when their representation
    norms differ (e.g. NLP hidden states, sentence/token embeddings, or any
    high-dimensional feature vectors where orientation matters more than
    magnitude).

    The last dimension is treated as the feature dimension along which
    cosine similarity is computed.

    Args:
        student_output: Feature tensor from the student model.
        teacher_output: Feature tensor from the teacher model.  Must be
            broadcastable to `student_output` along all dimensions except
            the last, and the last dimension must match.
        mask: Optional boolean mask of shape `student_output.shape[:-1]`.
            `True` positions are *kept*; `False` positions are ignored.

    Returns:
        Scalar loss equal to the mean of `1 - cosine_similarity` over all
        valid positions.

    Raises:
        ValueError: If the last dimension of the two tensors does not match,
            or if the mask shape is invalid.
    """
    if student_output.shape[-1] != teacher_output.shape[-1]:
        raise ValueError(
            "student_output and teacher_output must have the same last "
            "dimension (feature dim) for cosine KD."
        )

    student = student_output
    teacher = teacher_output.detach()

    if mask is not None:
        if mask.shape != student.shape[:-1]:
            raise ValueError("mask must match all output dimensions except the last.")
        valid_mask = mask.to(torch.bool)
        student = student[valid_mask]
        teacher = teacher[valid_mask]
    else:
        student = student.reshape(-1, student.shape[-1])
        teacher = teacher.reshape(-1, teacher.shape[-1])

    # cosine_similarity along feature dim, result shape: (N,)
    cos_sim = F.cosine_similarity(student, teacher, dim=-1)
    return (1.0 - cos_sim).mean()


def soft_bce_kd_loss(
    student_output: torch.Tensor,
    teacher_output: torch.Tensor,
    *,
    temperature: float = 1.0,
    mask: Optional[torch.Tensor] = None,
) -> torch.Tensor:
    """Computes soft Binary Cross-Entropy (BCE) Knowledge Distillation loss.

    Designed for multi-label classification where each label is an
    independent binary prediction. Unlike `logits_kd_loss` which uses
    softmax (assuming mutually exclusive classes), this function applies
    sigmoid to each logit independently, treating teacher outputs as soft
    Bernoulli targets.

    Temperature scaling softens the teacher's per-label probabilities:
    `sigmoid(teacher_logits / T)` produces targets closer to 0.5 at
    higher temperatures, exposing richer inter-label relationships to
    the student. The loss is scaled by `T²` to keep gradient magnitudes
    consistent with the supervised BCE loss, following the same convention
    as `logits_kd_loss`.

    Args:
        student_output: Raw logits from the student model, shape
            `(..., C)` where `C` is the number of labels.
        teacher_output: Raw logits from the teacher model, same shape as
            `student_output`.
        temperature: Temperature for softening teacher probabilities.
            Higher values produce softer targets. Defaults to 1.0.
        mask: Optional boolean mask.  Supported shapes:

            - `student_output.shape`: per-element mask (e.g. ignore
              specific label slots).
            - `student_output.shape[:-1]`: per-position mask (e.g.
              ignore padding tokens across all labels).

            `True` positions are *kept*; `False` positions are ignored.

    Returns:
        Scalar loss: mean of the temperature-scaled binary cross-entropy
        over all valid elements.

    Raises:
        ValueError: If student and teacher shapes do not match, or if the
            mask shape is invalid.
    """
    if student_output.shape != teacher_output.shape:
        raise ValueError(
            "student_output and teacher_output must have the same shape "
            "for binary KD."
        )

    student = student_output
    teacher = teacher_output.detach()

    soft_targets = torch.sigmoid(teacher / temperature)

    if mask is not None:
        valid_mask = mask.to(torch.bool)
        if mask.shape == student.shape:
            student = student[valid_mask]
            soft_targets = soft_targets[valid_mask]
        elif mask.shape == student.shape[:-1]:
            student = student[valid_mask]
            soft_targets = soft_targets[valid_mask]
        else:
            raise ValueError(
                "mask must match the full output shape or all dimensions "
                "except the last."
            )

    return (
        F.binary_cross_entropy_with_logits(
            student / temperature, soft_targets, reduction="mean"
        )
        * temperature ** 2
    )


class KDWeightScheduler:
    """Schedules the KD loss weight with optional delayed start and linear warmup.

    All parameters use the same training progress unit (epochs, optimizer
    steps, etc.).  Weight is `0` before `start`, ramps linearly from `0` to
    `target_weight` over `warmup_length`, then stays at `target_weight`.

    Args:
        target_weight: Final KD weight after warmup completes.
        start: Training position at which KD begins. Defaults to `0`.
        warmup_length: Duration over which the weight ramps from `0` to
            `target_weight`. `0` means no ramp. Defaults to `0`.

    Raises:
        ValueError: If `start` or `warmup_length` is negative.

    Example::

        scheduler = KDWeightScheduler(target_weight=1.0, start=10, warmup_length=20)
        for epoch in range(100):
            kd_loss_weight = scheduler.get_weight(epoch)
            # 0.0 for epoch 0–9, ramps 0→1 for epoch 10–29, 1.0 after
    """

    def __init__(
        self,
        target_weight: float,
        start: float = 0,
        warmup_length: float = 0,
    ) -> None:
        if start < 0:
            raise ValueError(f"`start` must be >= 0, got {start}")
        if warmup_length < 0:
            raise ValueError(f"`warmup_length` must be >= 0, got {warmup_length}")

        self.target_weight = target_weight
        self.start = start
        self.warmup_length = warmup_length

    def get_weight(self, current: float) -> float:
        """Returns the KD weight at the given training position.

        Args:
            current: Current training position (e.g. epoch or step count).
        """
        if current < self.start:
            return 0.0
        if self.warmup_length == 0:
            return self.target_weight
        warmup_progress = (current - self.start) / self.warmup_length
        return self.target_weight * min(warmup_progress, 1.0)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"target_weight={self.target_weight}, "
            f"start={self.start}, "
            f"warmup_length={self.warmup_length})"
        )

