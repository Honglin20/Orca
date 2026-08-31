# Vision CNN Rules

This ruleset applies to computer-vision CNN models that are being prepared for optimization and NAS supernet conversion.

## Rule: Adaptive Global Pooling Classifier Head

- name: Adaptive Global Pooling Classifier Head
- type: tradeoff
- description: Replace fixed spatial flattening and large fully connected classifier heads with adaptive global pooling followed by a channel-only classifier.
- pros: Supports variable input resolutions and supernet candidates that change intermediate spatial sizes; usually reduces classifier parameters substantially.
- cons: Removes spatially specific classifier weights and changes the head parameterization.

### Instruction

**When to apply**
- Apply when the classifier uses `flatten` over `C * H * W` followed by one or more `Linear` layers tied to a fixed feature-map size.
- Apply when stage stride, dilation, input resolution, or pooling candidates may change during supernet search.

**Implementation**
1. Replace fixed-size flattening with `nn.AdaptiveAvgPool2d(1)` for 2D image CNNs.
2. Flatten only the channel dimension after pooling.
3. Replace the first classifier `Linear(C * H * W, hidden_dim)` or `Linear(C * H * W, num_classes)` with `Linear(C, hidden_dim)` or `Linear(C, num_classes)`.
4. Preserve the final number of output classes and the public `forward` output shape.

```python
self.pool = nn.AdaptiveAvgPool2d(1)
self.classifier = nn.Linear(out_channels, num_classes)

def forward(self, x):
    x = self.features(x)
    x = self.pool(x).flatten(1)
    return self.classifier(x)
```

**Validation**
- Test multiple input resolutions that are valid for the CNN stem and stages.
- Confirm the output shape remains `[batch_size, num_classes]`.
- Check parameter count changes in the classifier head and treat any accuracy change as task-dependent.
